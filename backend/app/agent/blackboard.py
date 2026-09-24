"""黑板式共享状态的读写函数（技术方案 6.3）。

黑板的三条规则：

| 规则 | 含义 | 执行点 |
| --- | --- | --- |
| 单一写入者 | 一个槽位只有一个智能体有权写 | `write_slot` 查配置里的 `owner` |
| 只写不覆盖 | 高优先级来源可覆盖低优先级 | `write_slot` 比 `SOURCE_PRIORITY` |
| 带状态写入 | 写入必须带 `status` 与 `source` | `SlotValue` 的字段与校验器 |

三条都由代码执行，不写进提示词：提示词是建议，模型可以不遵守；而"档案里农户已
手动修正过的生育期被模型按播种时间推算的值悄悄覆盖"这类问题一旦发生，事后无法
从数据上发现。

**与 LangGraph 的配合**：本模块的写入是**原地修改** `state["slots"]`，这在纯函数
调用（单测、节点内部的局部判断）下是直观的；但 LangGraph 不会把节点内的原地修改
持久化给后续节点——节点必须把改动作为返回值交给图层合并。因此节点调用方需在返回
时带上 `{"slots": state["slots"]}`（T2-14 起的节点工厂负责这件事）。

本模块放在 `app/agent/` 而非 `state.py`：`state.py` 被 `app/process_config/models.py`
反向依赖取枚举，若在 `state.py` 里导入 `ProcessConfig` 就会形成循环导入。
"""

from app.agent.state import (
    SOURCE_PRIORITY,
    ConsultationState,
    ConsultStage,
    EvidenceItem,
    SlotSource,
    SlotValue,
)
from app.process_config.models import ProcessConfig


class BlackboardError(RuntimeError):
    """写入违反黑板规则时抛出。

    继承 `RuntimeError` 而不是 `ValueError`：它不是入参格式问题，而是"这个写入方
    没有写这个槽位的权限"这类协作契约被违反，属于调用逻辑错误。
    """


def write_slot(
    state: ConsultationState,
    slot: SlotValue,
    config: ProcessConfig,
    *,
    writer: str,
    plot_prefix: str | None = None,
) -> None:
    """按黑板三条规则写入槽位。

    低优先级写入**静默返回而不抛异常**是刻意选择：`inferred` 想覆盖 `profile` 是
    正常业务场景（系统按播种时间推算生育期，但档案里农户已手动修正过），这不是
    错误，只需忽略。若抛异常，正常路径也要写 `try`。

    多地块按技术方案 6.3 处理：`plot_prefix` 由调用方传入（如 `plot_1`），槽位键
    变成 `plot_1.tea_symptom_part`；当前问诊只针对一个地块，切换地块需开启新
    `session_id`。槽位规格仍按**未加前缀**的 `slot.key` 查，因为配置是地块无关的。
    """
    key = f"{plot_prefix}.{slot.key}" if plot_prefix else slot.key
    spec = config.slot_spec(slot.key)
    if spec is None:
        raise BlackboardError(f"槽位 {slot.key} 未在流程 {config.process_id.value} 中声明")
    if spec.owner != writer:
        raise BlackboardError(f"槽位 {slot.key} 的写入者为 {spec.owner}，{writer} 无权写入")

    existing = state.get("slots", {}).get(key)
    if existing is not None and existing.status == "filled":
        if SOURCE_PRIORITY[SlotSource(slot.source)] >= SOURCE_PRIORITY[SlotSource(existing.source)]:
            return  # 不高于已有来源的优先级不得覆盖，静默保留原值

    state.setdefault("slots", {})[key] = SlotValue(key=key, **slot.model_dump(exclude={"key"}))


def read_slot(state: ConsultationState, key: str, *, plot_prefix: str | None = None) -> SlotValue | None:
    """读槽位。未写入过的槽位返回 `None`，调用方据此判断"还没问过"。"""
    full_key = f"{plot_prefix}.{key}" if plot_prefix else key
    return (state.get("slots") or {}).get(full_key)


def slot_summary(state: ConsultationState) -> dict[str, str]:
    """把槽位压成 键 → 展示值 的映射，供提示词注入。

    只输出 `filled` 与 `unknown`，跳过 `missing`。`unknown` 用固定文案而不是空串，
    是为了让模型知道"问过了但答不出"，从而降低确定性表述而不是继续追问——这正是
    「未知是合法答案」在提示词侧的落地。
    """
    out: dict[str, str] = {}
    for key, slot in (state.get("slots") or {}).items():
        if slot.status == "filled":
            out[key] = slot.value or ""
        elif slot.status == "unknown":
            out[key] = "农户表示不清楚"
    return out


def set_stage(state: ConsultationState, stage: ConsultStage) -> dict:
    """返回推进环节所需的状态增量，由节点作为返回值交给图层合并。

    不直接改 `state["stage"]`：环节推进必须经过图的条件边判定，原地改会让
    checkpointer 里的状态与图的实际走向不一致。
    """
    return {"stage": stage}


def append_evidence(state: ConsultationState, items: list[EvidenceItem]) -> dict:
    """按 `knowledge_id` 去重后返回新的证据列表，由节点作为返回值提交。

    去重是必要的：同一轮重试会重复检索同一批知识条目，若不去重，`EvidenceSet`
    里会出现同一知识条目的多条副本，干扰后续重排与"引用几条证据"的展示计数。
    """
    merged = {e["knowledge_id"]: e for e in (state.get("evidence") or [])}
    for item in items:
        merged[item.knowledge_id] = item.model_dump(mode="json")
    return {"evidence": list(merged.values())}


__all__ = [
    "BlackboardError",
    "append_evidence",
    "read_slot",
    "set_stage",
    "slot_summary",
    "write_slot",
]
