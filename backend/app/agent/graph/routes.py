"""条件边函数：把「下一个节点是谁」的全部判断集中在此（T2-24）。

主管式确定性编排的核心是状态机本身，不是模型（技术方案 6.2）。因此路由必须是
**可单测、可确定性重放、可断点回溯**的纯函数：只读 `ConsultationState`，不做 IO，
不调模型。任何一次模型调用的结果抖动，都不允许改变同一条路径的去向。

四条硬性约束（违反后的失败模式写在括号里）：

1. 函数体内不出现 `await`、`db`、`gateway`、`httpx`（重放结果不确定，检查点恢复后
   会走到不同分支）。
2. 每个分支都必须在同名 `*_MAP` 里列出，未覆盖时抛 `UnroutedStateError` 而不是
   `return "default"`（新增流程后静默走默认分支，路由错误被吞掉）。
3. 只读枚举与布尔，禁止对 `reply`、`summary`、`message` 做关键词匹配（自由文本抖动
   导致同一输入两次路由不一致）。
4. 首行检查 `state["redlines_loaded"]`，为假即 fail-closed 转人工（用通用规则代替
   流程红线继续出方，突破产品说明书 9.3）。

子图的边不能指向父图节点，这是 LangGraph 的硬约束：同一个返回名 `handoff` 在主图
path_map 里指向 `handoff` 节点，在子图 path_map 里必须指向子图出口 `"__end__"`。
四个子图 path_map 分别为 `ROUTE_AFTER_SUBGRAPH_GUARD`、`ROUTE_AFTER_INTERVIEW`、
`ROUTE_AFTER_JUDGE`（各一次 `handoff`），`ROUTE_AFTER_SUBGRAPH` 是主图边。

本模块顶部只导入状态与枚举：不导入节点、服务或数据库对象，这样单测可以在纯内存下
跑，且导入本模块不会拉起任何外部依赖。
"""

from collections.abc import Mapping
from typing import Any

from app.agent.state import ConsultationState, ConsultStage, ProcessId, SlotValue

# 会诊（多视角并行）只在指定流程启用：茶、辣椒、杂草属"同症多因"的高混淆场景，
# 需要多个独立视角后再收敛；水稻与养猪的判别依据更确定，多跑一轮会诊只增成本。
PANEL_ENABLED_PROCESSES = frozenset({ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED})

# `UnroutedStateError` 消息里带上的关键字段：只取决定路由的那几项，避免把整个
# state（含用户原文、档案快照）写进日志。
_SNAPSHOT_KEYS = (
    "stage",
    "process_id",
    "process_locked",
    "redlines_loaded",
    "round",
    "slot_round",
)


def _snapshot(state: ConsultationState) -> dict[str, str]:
    """取 state 关键字段的可读快照，供异常消息与日志使用。"""
    return {key: repr(state.get(key)) for key in _SNAPSHOT_KEYS}


class UnroutedStateError(RuntimeError):
    """条件边遇到未被 path_map 覆盖的 state 组合时抛出。

    路由不允许 `return "default"`：静默兜底会把"新增流程忘了接线"这类错误藏到线上
    输出错误处方的那一刻。消息里带函数名与关键字段快照，检查点回溯时能直接定位。
    """

    def __init__(self, func_name: str, state: ConsultationState) -> None:
        self.func_name = func_name
        self.state_snapshot = _snapshot(state)
        super().__init__(f"{func_name} 未覆盖的 state 组合: {self.state_snapshot}")


def _slot_status(slot: SlotValue | dict[str, Any]) -> str:
    """取槽位状态。state 里是 `SlotValue`，检查点反序列化后可能是同形 dict。"""
    if isinstance(slot, dict):
        return slot.get("status", "missing")
    return slot.status


def slots_ready(state: ConsultationState) -> bool:
    """必填槽位是否齐备：只做集合运算，不查流程配置。

    数据来源是 `state["required_slots"]`（intake 写入的必填键清单）与 `state["slots"]`。
    `unknown`（问过且农户答不出）是**合法答案**，与 `filled` 同等视为已解决——把它
    当缺口会让流程永远收敛不了，反复追问同一件农户答不出的信息。
    """
    required = set(state.get("required_slots") or [])
    slots = state.get("slots") or {}
    resolved = {key for key, slot in slots.items() if _slot_status(slot) in {"filled", "unknown"}}
    return not (required - resolved)


ROUTE_AFTER_INTAKE = {"triage": "triage", "enter_process": "subgraph_router", "handoff": "handoff"}


def route_after_intake(state: ConsultationState) -> str:
    """受理后的第一条路由：受理即分流，先定流程再问问题。

    `process_locked` 为真说明入口已直达锁定流程（如小程序「杂草防治」按钮带的
    `process_hint`），此时不必再判流程；否则进 triage。绝不允许先问农户几句再定流程，
    那会把成本花在后续必然重问的信息上。
    """
    if not state.get("redlines_loaded", False):
        return "handoff"
    if state.get("process_locked") and state.get("process_id") is not None:
        return "enter_process"  # 入口直达（process_hint）已锁定流程
    return "triage"


ROUTE_AFTER_TRIAGE = {
    "confirm_object": "confirm_object",  # 对象待确认，插入一次确认交互
    "report_guard": "report_guard",  # 信号命中，先过主图入口守卫
    "enter_process": "subgraph_router",  # 已确认，落到具体流程子图
    "handoff": "handoff",
}


def route_after_triage(state: ConsultationState) -> str:
    """分诊后的路由：纯函数，无 IO，无模型调用。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    triage = state.get("triage") or {}
    if triage.get("handoff_to_report"):
        return "report_guard"  # 报告信号命中，先过守卫再谈其他
    if not triage.get("object_confirmed"):
        return "confirm_object"  # 对象待确认，插入一次确认交互
    return "enter_process"  # 含养猪：其子图入口守卫负责 pre-observe 检查


ROUTE_AFTER_CONFIRM_OBJECT = {"triage": "triage", "enter_process": "subgraph_router", "handoff": "handoff"}


def route_after_confirm_object(state: ConsultationState) -> str:
    """确认对象后的路由：农户未确认或与档案冲突则回分诊重判。

    冲突只能回分诊，不能就地改流程：流程归属一旦被无条件改写，档案与本次结论就
    不再一致，后续按流程红线出的处方也就失去了依据。
    """
    if not state.get("redlines_loaded", False):
        return "handoff"
    if state.get("object_confirmed_conflict", False):
        return "triage"
    if state.get("process_id") is None:
        raise UnroutedStateError("route_after_confirm_object", state)
    return "enter_process"


ROUTE_AFTER_REPORT_GUARD = {"enter_process": "subgraph_router", "confirm_object": "confirm_object", "handoff": "handoff"}


def route_after_report_guard(state: ConsultationState) -> str:
    """主图入口守卫后的路由：命中即转人工，否则进流程子图。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"
    return "confirm_object" if state.get("process_id") is None else "enter_process"


ROUTE_AFTER_SUBGRAPH_GUARD = {"observe": "observe", "interview": "interview", "handoff": "__end__"}


def route_after_subgraph_guard(state: ConsultationState) -> str:
    """子图内守卫后的路由：命中就出子图，否则按 stage 进 observe 或 interview。

    同一个函数服务两处检查（技术方案 5.3.7）：养猪流程的检查在 `observe` **之前**
    （入口守卫），此时 stage 仍是 INTAKE/TRIAGE，放行后去 `observe`；其余流程的检查
    在 `observe` **之后**，去 `interview`。用 stage 区分两处，不需要两个函数。
    """
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"  # handoff 在子图 path_map 里即出口
    if state.get("stage") in (ConsultStage.INTAKE, ConsultStage.TRIAGE):
        return "observe"  # 养猪：入口守卫放行后才看现场
    return "interview"  # 其余流程：observe 之后的兜底检查


ROUTE_AFTER_SUBGRAPH = {"gate": "gate", "handoff": "handoff"}


def route_after_subgraph(state: ConsultationState) -> str:
    """子图出口路由：只有走到 prescribe 才算正常完成，其余一律转人工。

    这是子图退出的唯一判据。子图可能因为预算耗尽、守卫命中、问诊未收敛等十余种原因
    提前退出，若按"有没有 diagnosis"之类宽松条件放行，这些提前退出就会误入闸门，
    拿到一份根本没有处方的 GateContext。
    """
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    diagnosis = state.get("diagnosis") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"
    if diagnosis.get("out_of_scope"):
        return "handoff"
    return "gate" if state.get("stage") == ConsultStage.PRESCRIBE else "handoff"


ROUTE_AFTER_INTERVIEW = {"interview": "interview", "verify": "verify", "panel": "panel", "handoff": "__end__"}


def route_after_interview(state: ConsultationState) -> str:
    """问诊后路由：命中报告信号先转人工；疑似用药先会诊；槽位齐备推进。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"  # 5.3.7：问诊之后的第二次守卫检查
    budget = state.get("budget") or {}
    if slots_ready(state):
        if state.get("process_id") in PANEL_ENABLED_PROCESSES and state.get("panel_needed"):
            return "panel"
        return "verify"
    if budget.get("interview_rounds", 0) >= budget.get("max_interview_rounds", 4):
        return "handoff"  # 连续未收敛，输出携带已确认信息
    return "interview"


ROUTE_AFTER_JUDGE = {"interview": "interview", "prescribe": "prescribe", "handoff": "__end__"}


def route_after_judge(state: ConsultationState) -> str:
    """判定后的路由：超边界或回环用尽则转人工，否则可开方。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    diagnosis = state.get("diagnosis") or {}
    budget = state.get("budget") or {}
    if diagnosis.get("out_of_scope"):
        return "handoff"  # 超能力边界，直达转人工
    level = diagnosis.get("confidence_level")
    if level == "uncertain" and (diagnosis.get("needs_more_info") or []):
        if budget.get("judge_loops", 0) < budget.get("max_judge_loops", 2):
            return "interview"  # 置信不足且还有问可问，反向补问
        return "handoff"
    if level in ("likely", "preliminary"):
        return "prescribe"
    raise UnroutedStateError("route_after_judge", state)


ROUTE_AFTER_GATE = {"followup": "followup", "handoff": "handoff"}


def route_after_gate(state: ConsultationState) -> str:
    """门禁后的路由：allow 去登记回访，degrade/block 一律转人工。

    此处不查 `state["redlines_loaded"]` 而查 `gate_result["process_redlines_loaded"]`：
    门禁结论是权威来源，state 上的 `redlines_loaded` 只是入口快照，两者不一致时以门禁
    为准——闸门是 fail-closed 组件，它的判定必须压过入口时的一次性检查。
    """
    gate = state.get("gate_result") or {}
    if not gate.get("process_redlines_loaded", False):
        return "handoff"  # fail-closed
    decision = gate.get("decision")
    if decision == "allow":
        return "followup"
    if decision in ("degrade", "block"):
        return "handoff"
    raise UnroutedStateError("route_after_gate", state)


# 路由函数与其 path_map 的绑定表。新增路由时两处一起改，`test_routes.py` 逐一核对
# 每个函数的可能返回值集合与对应 path_map 的键集合相等，漏改会当场失败。
ROUTE_MAPS: tuple[tuple[object, Mapping[str, str]], ...] = (
    (route_after_intake, ROUTE_AFTER_INTAKE),
    (route_after_triage, ROUTE_AFTER_TRIAGE),
    (route_after_confirm_object, ROUTE_AFTER_CONFIRM_OBJECT),
    (route_after_report_guard, ROUTE_AFTER_REPORT_GUARD),
    (route_after_subgraph_guard, ROUTE_AFTER_SUBGRAPH_GUARD),
    (route_after_subgraph, ROUTE_AFTER_SUBGRAPH),
    (route_after_interview, ROUTE_AFTER_INTERVIEW),
    (route_after_judge, ROUTE_AFTER_JUDGE),
    (route_after_gate, ROUTE_AFTER_GATE),
)

__all__ = [
    "PANEL_ENABLED_PROCESSES",
    "ROUTE_AFTER_CONFIRM_OBJECT",
    "ROUTE_AFTER_GATE",
    "ROUTE_AFTER_INTAKE",
    "ROUTE_AFTER_INTERVIEW",
    "ROUTE_AFTER_JUDGE",
    "ROUTE_AFTER_REPORT_GUARD",
    "ROUTE_AFTER_SUBGRAPH",
    "ROUTE_AFTER_SUBGRAPH_GUARD",
    "ROUTE_AFTER_TRIAGE",
    "ROUTE_MAPS",
    "UnroutedStateError",
    "route_after_confirm_object",
    "route_after_gate",
    "route_after_intake",
    "route_after_interview",
    "route_after_judge",
    "route_after_report_guard",
    "route_after_subgraph",
    "route_after_subgraph_guard",
    "route_after_triage",
    "slots_ready",
]
