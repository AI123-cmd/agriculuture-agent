"""离线评测集运行器（T6-01，技术方案 14.1 第一层）。

一次评测 = 把一道题喂进**编译好的整图**，跑到图停下，再从终态抽出可比对的中间量
（`EvalResult`）。设计上的三个关键点：

1. **跑整图而不是跑单节点**：14.1 的第一条断言是"受理分流必须正确"，只有端到端跑才能
   验证"受理即分流"没有退化成"先问症状再猜流程"。
2. **必须处理中断**：问诊节点在 `interrupt` 处暂停等农户回答（T2-32），闸门在 degrade/block
   处暂停等知情确认。运行器因此是一个"`astream` → 命中中断就 `Command(resume=...)` 续跑"
   的循环，而不是一次 `ainvoke` 就完事。
3. **抽取层映射口径**：评测口径的 `rules_hit` / `conclusion_key` 对应生产的
   `GateResult.checked_rules` / `DiagnosisResult.conclusion`。映射只发生在本模块，
   生产契约、DB 字段名一概不动（见 `schema.py` 模块说明）。

`build_initial_state` 复刻 `AIService._inject_config` + `_resume_or_start` 的注入口径：
它把路由函数与闸门依赖的五个配置事实键（`redlines_loaded` / `required_slots` / `redlines` /
`config_version` / `config_hash`）写进初始 state。**刻意不锁流程**：评测要验证分诊的路由，
锁死 `process_hint` 就等于跳过被验证的环节，故基线取 `general`，让 `triage` 自己判流程。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core.config import get_settings
from app.process_config.loader import get_process_config
from app.process_config.redlines import RedlineValidator
from app.process_config.version import build_ref

from .report import DEFAULT_DROP_PP
from .schema import EvalCase, EvalGate, EvalInputs, EvalResult

# 续跑上限。中断次数有限（问诊轮次上限 + 闸门确认各一次），加一个上限是防止"恢复值被
# 忽略导致图原地打转"这类实现错误把评测卡死——宁可失败也不无限循环。
_MAX_RESUME_ROUNDS = 16

# 分集权重文件（T6-02）。缺失时全部按 1.0 计，评测仍可跑（不因权重文件缺失而失败）。
WEIGHTS_PATH = Path(__file__).resolve().parent / "cases" / "weights.toml"


def load_weights(path: Path | None = None) -> dict[str, float]:
    """读分集权重。文件缺失或没有 `[weights]` 段时返回空表（调用方按 1.0 兜底）。

    权重是"改了要能看出来"的资产：`general` 的 1.5 不是常量而是文件里的数据，改它应当
    改变加权一致率（T6-02 验收要求）。因此这里从文件读，不写死在代码里。
    """
    target = path if path is not None else WEIGHTS_PATH
    if not target.exists():
        return {}
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    rows = data.get("weights") or {}
    return {str(key): float(value) for key, value in rows.items()}


def load_drop_pp(path: Path | None = None) -> float:
    """读相对门禁阈值（百分点）。缺失时返回 `DEFAULT_DROP_PP`。"""
    target = path if path is not None else WEIGHTS_PATH
    if not target.exists():
        return DEFAULT_DROP_PP
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    thresholds = data.get("thresholds") or {}
    return float(thresholds.get("consistency_drop_pp", DEFAULT_DROP_PP))


def weighted_consistency(per_process: dict[str, tuple[int, int]], weights: dict[str, float]) -> float:
    """按分集权重汇总一致率：Σ(权重 × 通过数) / Σ(权重 × 总数)。

    权重缺省为 1.0。分母为 0 时返回 1.0（没有用例不构成失败，也不能除零）。
    加权而非简单平均是 T6-02 的核心口径：`general` 是错误蓄水池，其失分不该与占位流程
    的失分等权。
    """
    numerator = denominator = 0.0
    for process_id, (passed, total) in per_process.items():
        weight = weights.get(process_id, 1.0)
        numerator += weight * passed
        denominator += weight * total
    if denominator == 0:
        return 1.0
    return numerator / denominator


def build_initial_state(inputs: EvalInputs, *, process_id: Any = None) -> dict[str, Any]:
    """把评测输入组装成图初始 state，并注入流程配置事实。

    与生产入口（`AIService._resume_or_start` + `_inject_config`）保持同一口径：
    `process_hint` 留空（评测不锁流程，交由分诊判定），配置基线取 `general`。
    `inputs.management` 与 `inputs.env_reading` 是评测集为便于阅读而分列的三类输入，
    图内并没有单独的键承载，统一放进 `context` 供节点按需读取。
    """
    from app.agent.state import ProcessId

    pid = process_id or ProcessId.GENERAL
    config = get_process_config(pid)
    loaded = RedlineValidator().validate(config)
    return {
        "session_id": f"eval-{inputs.message[:8]}",
        "user_id": "eval-user",
        "process_hint": None,
        "user_message": inputs.message,
        "context": {"management": inputs.management, "env_reading": inputs.env_reading},
        "messages": [{"role": "user", "content": inputs.message}],
        "redlines_loaded": loaded.loaded,
        "required_slots": config.required_slot_keys(),
        "redlines": loaded.model_dump(mode="json"),
        "config_version": config.version,
        "config_hash": build_ref(config).content_hash,
    }


def _resume_value(payload: object, case: EvalCase) -> object:
    """按中断类型给出恢复值。

    - 问诊中断（载荷带 `questions`）：把评测集预置的答案摊成 `answers` 列表交给
      `apply_answers`（键取 `slotKey`，与接口契约一致）。
    - 闸门确认中断（`kind == "gate_confirm"`）：固定传 `"continue"`，让图把降级/拦截
      文案走完，评测关心的是最终文案与闸门命中，而不是这次交互本身。
    """
    if isinstance(payload, dict) and payload.get("kind") == "gate_confirm":
        return "continue"
    answers = [{"slotKey": key, "value": value} for key, value in case.inputs.answers.items()]
    return {"answers": answers}


def _eval_gate(raw: object) -> EvalGate:
    """把生产 `GateResult` 序列化体映射成评测镜像 `EvalGate`。

    **`rules_hit` 取违规命中的规则 id（`violations[*].rule_id`），不是生产
    `checked_rules`。** 两者语义不同：`checked_rules` 记录"执行过哪些规则"（一次完整
    判定几乎等于全部十二条），`check_rules` 若拿来做 `expect_gate_rules` 断言，任何合法
    规则名都会恒真，正是技术方案 14.1 警告的"最隐蔽的评测失效"。评测口径的"命中"是
    "这条规则拦下了这份处方"，对应 `violations`。

    图未走到闸门时（如提前转人工）返回一个"未命中任何规则"的空壳，使
    `set(expect_gate_rules) <= set(result.gate.rules_hit)` 在"未期望规则"的题上成立。
    """
    if not isinstance(raw, dict):
        return EvalGate(passed=True, decision="allow", rules_hit=[], violations=[])
    violations = list(raw.get("violations") or [])
    hit = sorted({str(v.get("rule_id")) for v in violations if isinstance(v, dict) and v.get("rule_id")})
    return EvalGate(
        passed=bool(raw.get("passed", False)),
        decision=raw.get("decision", "allow"),
        rules_hit=hit,
        violations=violations,
    )


def _extract_sources(state: dict[str, Any]) -> list[str]:
    """抽取"引用来源"集合（技术方案附录 A.1 的 `SourceType`）。

    生产代码没有 `SourceType` 枚举，这里的口径按"处方实际用到了哪几类依据"反推：
    流程配置凡是出方的题都用到了（问诊清单、处方模板都来自它）；有知识库证据条目即计入
    `knowledge_base`；环境读数真被采到才计入 `env_reading`。`model_inference` 不作为默认
    来源，需要它的题在 `allowed_sources` 里显式声明。
    """
    sources = {"process_config"}
    if state.get("evidence"):
        sources.add("knowledge_base")
    observations = state.get("observations") or {}
    if observations.get("env_source") not in (None, "none", "sensor_stale"):
        sources.add("env_reading")
    return sorted(sources)


def extract_result(
    state: dict[str, Any],
    *,
    node_path: list[str],
    asked_slots: list[str],
) -> EvalResult:
    """从图终态抽取 `EvalResult`。这是评测口径与生产口径的唯一映射点。"""
    from app.agent.state import ProcessId

    raw_pid = state.get("process_id")
    try:
        process_id = ProcessId(raw_pid) if raw_pid is not None else ProcessId.GENERAL
    except ValueError:
        process_id = ProcessId.GENERAL

    diagnosis = state.get("diagnosis") or {}
    budget = state.get("budget") or {}
    return EvalResult(
        process_id=process_id,
        asked_slots=asked_slots,
        conclusion_key=str(diagnosis.get("conclusion") or ""),
        gate=_eval_gate(state.get("gate_result")),
        final_text=str(state.get("prescription_text") or state.get("reply") or ""),
        sources=_extract_sources(state),
        rounds=int(budget.get("interview_rounds", 0)),
        node_path=node_path,
    )


class EvalRunner:
    """把评测题跑进整图并抽取结果。`graph` 必须是已编译且绑定 checkpointer 的图。"""

    def __init__(
        self,
        graph: CompiledStateGraph,
        knowledge_version: str,
        temperature: float = 0.0,
    ) -> None:
        self.graph = graph
        self.knowledge_version = knowledge_version
        self.temperature = temperature

    @property
    def model_mode(self) -> str:
        """本次评测的模型口径：`mock` 或 `live`。写进报告，便于区分回归来源。"""
        return "mock" if get_settings().USE_MOCK_LLM else "live"

    async def run(self, case: EvalCase) -> EvalResult:
        """跑一道题：`astream` 收集路径与产出，命中中断就恢复，直到图自然结束。"""
        config: RunnableConfig = {
            "configurable": {"thread_id": case.case_id},
            "recursion_limit": 100,
        }
        state = build_initial_state(case.inputs)
        merged, node_path, asked_slots = await self._drive(state, config, case)
        return extract_result(merged, node_path=node_path, asked_slots=asked_slots)

    async def _drive(
        self,
        state: dict[str, Any],
        config: RunnableConfig,
        case: EvalCase,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """执行循环：`astream(updates)` 逐节点累积，遇中断用 `Command` 续跑。"""
        merged: dict[str, Any] = dict(state)
        node_path: list[str] = []
        asked: list[str] = []
        seen_asked: set[str] = set()
        current: object = state

        for _ in range(_MAX_RESUME_ROUNDS):
            interrupts: object = None
            async for chunk in self.graph.astream(current, config=config, stream_mode="updates"):
                for key, value in chunk.items():
                    if key == "__interrupt__":
                        interrupts = value
                        continue
                    node_path.append(key)
                    if not isinstance(value, dict):
                        continue
                    merged.update(value)
                    _collect_asked(value, asked, seen_asked)
            if not interrupts:
                break
            payload = _first_interrupt_value(interrupts)
            current = Command(resume=_resume_value(payload, case))
        return merged, node_path, asked


def _first_interrupt_value(interrupts: object) -> object:
    """取第一个中断的中断载荷。`astream` 给出的是 `Interrupt` 元组。"""
    if isinstance(interrupts, (list, tuple)) and interrupts:
        return getattr(interrupts[0], "value", None)
    return getattr(interrupts, "value", None)


def _collect_asked(update: dict[str, Any], asked: list[str], seen: set[str]) -> None:
    """从节点产出里累积"问出过的槽位"：待答问题与已落槽位都算问过。"""
    for row in update.get("pending_questions") or []:
        if isinstance(row, dict):
            key = row.get("slot_key") or row.get("slotKey")
            if isinstance(key, str) and key and key not in seen:
                seen.add(key)
                asked.append(key)
    for key, slot in (update.get("slots") or {}).items():
        status = slot.get("status") if isinstance(slot, dict) else getattr(slot, "status", None)
        if status in ("filled", "unknown") and key not in seen:
            seen.add(key)
            asked.append(key)


__all__ = [
    "EvalRunner",
    "build_initial_state",
    "extract_result",
    "load_drop_pp",
    "load_weights",
    "weighted_consistency",
]
