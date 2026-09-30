"""提示词构造用的纯函数格式化工具（T2-60）。

本模块只做一件事：把流程配置对象与状态片段渲染成提示词里的文本。所有函数都是**纯函数**，
输入配置对象、输出字符串，无副作用、无外部依赖、不修改入参。因此提示词的构造可被完整单测：
给一份配置，断言生成的文本含哪些片段；相同输入两次调用必然返回相同结果。

术语两套分开（技术方案 9.1）：`format_terms` 用 `display_metrics`（内部规范口径），
`format_colloquial` 用 `colloquial_map`（农户能懂的说法）。两者必须分开，展示层渲染时必须过
`colloquial_map`。

提示词里**不写任何农技事实**（药剂名、剂量、阈值）——事实由知识库检索后注入候选区或由代码
在处方里填充，本模块只负责"如何组织语言"与"有哪些约束"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from app.agent.blackboard import slot_summary
from app.agent.contracts import PesticideItem
from app.agent.state import ConsultationState, ProcessId
from app.process_config.models import ConfusablePair, ProcessConfig, RedlineSpec

# 药剂剂量两端都缺时的兜底写法。集中声明，`format_dosage` 与提示词里的"不可增删"约束共用。
_DOSAGE_UNIT_FALLBACK = "按标签标注用量"


class CandidateView(Protocol):
    """候选方向的最小视图。字段与 T2-17 的 `CandidateSlot` 一一对应。

    用协议而不是直接导入 `CandidateSlot`，避免 `formatters` 反向依赖 `agents` 包——格式化
    只需读取这四个属性，不关心候选池的具体实现。
    """

    slot_key: str
    display_label: str
    group: str
    required: bool
    options: list[str] | None


def format_terms(display_metrics: list[str]) -> str:
    """内部术语口径。给模型与知识库用，不面向农户。"""
    return "、".join(display_metrics) or "无特殊口径"


def format_colloquial(colloquial_map: dict[str, str]) -> str:
    """面向农户的口语化对照。展示层据此替换内部术语。"""
    return "；".join(f"{term} 说成 {spoken}" for term, spoken in colloquial_map.items()) or "按日常说法"


def format_redlines(redlines: list[RedlineSpec]) -> str:
    """流程红线。只给编号与说明：规则细节属闸门职责，此处的作用是让模型不做无谓尝试。"""
    return "；".join(f"{item.id} {item.desc}" for item in redlines) or "无"


def format_confusables(pairs: list[ConfusablePair]) -> str:
    """易混淆项及其决定性特征。特征必须一起给出：只给候选名，模型只会反复问"是哪一种"。"""
    if not pairs:
        return "无"
    parts: list[str] = []
    for pair in pairs:
        features = "；".join(f"{name}：{'、'.join(pair.decisive_features.get(name, []))}" for name in pair.candidates)
        parts.append(f"{'与'.join(pair.candidates)}（{features}）")
    return " / ".join(parts)


def format_candidates(candidates: Sequence[CandidateView]) -> str:
    """候选追问方向清单。模型只能从这些方向选，不得超出。

    候选为空时给出"应进入下一环节"的提示：该流程已收敛，没有可问的方向，应由主图推进，
    而不是让模型自由发挥生成一个流程外的问题。
    """
    if not candidates:
        return "（本轮无候选问题，应进入下一环节）"
    lines: list[str] = []
    for candidate in candidates:
        kind = "必问" if candidate.required else "选问"
        options = f"，选项：{'、'.join(candidate.options)}" if candidate.options else ""
        lines.append(f"- {candidate.display_label}（槽位 {candidate.slot_key}，{candidate.group}，{kind}{options}）")
    return "\n".join(lines)


def format_slots(config: ProcessConfig, state: ConsultationState) -> str:
    """已确认与"答不出"的槽位。用口语标签而不是槽位键：键名对模型是噪声，对农户不可读。"""
    summary = slot_summary(state)
    if not summary:
        return "无"
    parts: list[str] = []
    for key, value in summary.items():
        spec = config.slot_spec(key)
        parts.append(f"{spec.display_label if spec else key}={value}")
    return "；".join(parts)


def format_observations(observations: object) -> str:
    """环境事实摘要。state 里存的是 `ObservationResult` 序列化后的字典。"""
    if not isinstance(observations, dict) or not observations:
        return "无"
    parts = [f"环境数据来源={observations.get('env_source') or '未知'}"]
    facts = observations.get("observed_facts")
    if isinstance(facts, dict) and facts:
        parts.append("现场事实：" + "；".join(f"{key}={value}" for key, value in facts.items()))
    note = observations.get("note")
    if note:
        parts.append(f"环境说明：{note}")
    return "；".join(parts)


def format_evidence(evidence: list[dict], *, max_rows: int = 6, content_cap: int = 120) -> str:
    """证据摘要：标题加内容片段。

    只给标题与内容摘要，不给条目 ID 与分值：判定要引用的是判据本身，条目 ID 由代码在组装
    处方时使用，分值属检索内部信息，注入它会让模型把分数写进结论。条数与内容长度由调用方
    给出，长上下文会稀释指令遵循度（技术方案 10.4）。
    """
    if not evidence:
        return "无（知识库未命中相关条目）"
    lines: list[str] = []
    for item in evidence[:max_rows]:
        title = item.get("title") or item.get("knowledge_id") or "未命名条目"
        content = str(item.get("content") or "")[:content_cap]
        lines.append(f"- {title}：{content}")
    return "\n".join(lines)


def format_history(history: object) -> str:
    """档案用药史。字段形态由档案快照决定，这里只做通用展平而不假设具体键名。"""
    if not isinstance(history, list) or not history:
        return "无"
    parts: list[str] = []
    for item in history:
        if isinstance(item, dict):
            parts.append("；".join(f"{key}={value}" for key, value in item.items()) or "无")
        else:
            parts.append(str(item))
    return " / ".join(parts)


def format_diagnosis(diagnosis: object) -> str:
    """判定结论摘要。state 里存的是 `DiagnosisResult` 序列化后的字典。"""
    if not isinstance(diagnosis, dict) or not diagnosis:
        return "无（判定未产出结论）"
    parts = [f"结论={diagnosis.get('conclusion') or '未知'}"]
    level = diagnosis.get("confidence_level")
    if level:
        parts.append(f"把握程度={level}")
    basis = diagnosis.get("basis")
    if isinstance(basis, list) and basis:
        parts.append("依据：" + "；".join(str(item) for item in basis))
    return "；".join(parts)


def format_dosage(item: PesticideItem) -> str:
    """剂量区间的可读写法。两端都缺时给一句"按标签标注用量"而不是空串。"""
    unit = item.dosage_unit or ""
    if item.dosage_min is not None and item.dosage_max is not None:
        return f"用量 {item.dosage_min}~{item.dosage_max} {unit}".strip()
    if item.dosage_min is not None:
        return f"用量不低于 {item.dosage_min} {unit}".strip()
    if item.dosage_max is not None:
        return f"用量不高于 {item.dosage_max} {unit}".strip()
    return _DOSAGE_UNIT_FALLBACK


def format_pesticide_candidates(candidates: list[PesticideItem]) -> str:
    """候选药剂清单，逐项给出全部事实字段。

    字段必须一起给出：只给药剂名，模型为了"说得完整"就会自己补一个剂量。给全剂量、间隔期
    与每季上限之后，"照抄"是它唯一能做的事。
    """
    if not candidates:
        return "无（本流程不提供药剂候选，请只给观察与求助建议）"
    lines: list[str] = []
    for item in candidates:
        extras = [
            f"安全间隔期 {item.safety_interval_days} 天" if item.safety_interval_days is not None else "",
            f"每季最多 {item.max_uses_per_season} 次" if item.max_uses_per_season is not None else "",
            f"登记作物 {'、'.join(item.registered_crops)}" if item.registered_crops else "",
            f"禁忌搭配 {'、'.join(item.forbidden_combinations)}" if item.forbidden_combinations else "",
        ]
        detail = "，".join(part for part in extras if part)
        lines.append(
            f"- {item.name}（条目 {item.knowledge_id}）：{format_dosage(item)}{'，' + detail if detail else ''}"
        )
    return "\n".join(lines)


def format_process_keywords(configs: Mapping[ProcessId, ProcessConfig]) -> str:
    """七条流程的识别词清单。每条别名只取前八个。

    技术方案 10.4 指出长上下文会降低指令遵循度，而流程别名是长列表里最不具区分度的部分：
    截断到八个既能覆盖主要表述，也不至于让提示词膨胀。可选流程清单来自流程配置而不是硬编码
    表，新增一条流程只需加一份 YAML，识别词自动进入提示词。
    """
    lines: list[str] = []
    for pid in ProcessId:
        config = configs.get(pid)
        if config is None:
            continue
        aliases = "、".join(config.object_aliases[:8]) or "无"
        lines.append(f"{pid.value}（{config.display_name}）：{aliases}")
    return "\n".join(lines)


def format_entries(entries: object) -> str:
    """档案经营条目的可读摘要。

    档案结构由调用方注入，本层不导入它的模型，故只做形态兼容：条目列表、单个条目、或缺失。
    """
    if not entries:
        return "无"
    items: list[object]
    if isinstance(entries, dict):
        items = [entries]
    elif isinstance(entries, list):
        items = list(entries)
    else:
        return str(entries)
    parts: list[str] = []
    for entry in items:
        if isinstance(entry, dict):
            process = entry.get("process_id") or "未指定"
            started = entry.get("start_date") or "未记录"
            parts.append(f"{process}（起于 {started}）")
        else:
            parts.append(str(entry))
    return "、".join(parts) or "无"


__all__ = [
    "CandidateView",
    "format_candidates",
    "format_colloquial",
    "format_confusables",
    "format_diagnosis",
    "format_dosage",
    "format_entries",
    "format_evidence",
    "format_history",
    "format_observations",
    "format_pesticide_candidates",
    "format_process_keywords",
    "format_redlines",
    "format_slots",
    "format_terms",
]
