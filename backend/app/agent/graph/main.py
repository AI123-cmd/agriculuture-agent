"""主图组装：受理即分流 + 七条流程子图 + 闸门 + 回环出口（T2-25，技术方案 7.1）。

本模块是编排层的唯一装配点。三条不可妥协的约束：

1. **受理即分流**：`START → intake` 之后的第一条边就是条件边，第二条 trace 元素必然是
   `triage` 或 `subgraph_router`，不存在"先问症状再猜流程"的中间环节。
2. **子图的边不指向父图**：这是 LangGraph 硬约束。子图内部守卫命中时只能走子图出口
   `__end__`，再由主图的 `route_after_subgraph` 统一导向 `handoff`（见 routes.py 注释）。
3. **闸门在两个节点之间**：`prescribe` 由子图产出，子图交回主图后经 `gate` 子图判定，
   `allow` 去 `followup`、`degrade`/`block` 去 `handoff`。闸门是独立子图，见 gate_graph.py。
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from typing import cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph, StateNode

from app.agent.agents.handoff import build_handoff_node
from app.agent.agents.report_guard import build_report_guard_node
from app.agent.agents.triage import run_triage
from app.agent.budget import ConsultBudget
from app.agent.checkpointer import get_checkpointer
from app.agent.graph.routes import (
    ROUTE_AFTER_CONFIRM_OBJECT,
    ROUTE_AFTER_GATE,
    ROUTE_AFTER_INTAKE,
    ROUTE_AFTER_REPORT_GUARD,
    ROUTE_AFTER_SUBGRAPH,
    ROUTE_AFTER_TRIAGE,
    UnroutedStateError,
    route_after_confirm_object,
    route_after_gate,
    route_after_intake,
    route_after_report_guard,
    route_after_subgraph,
    route_after_triage,
)
from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.state import ConsultationState, ConsultStage, ProcessId

# 主图节点一律只吃 state、返回 state 增量，不带 `config` 形参：LangGraph 会把运行时
# `RunnableConfig` 按形参名注入，而主图这些节点不需要它（与 `handoff`/`report_guard`
# 节点的形态一致，也避免 LangGraph 对 `config` 参数类型发出告警）。
#
# 两处 LangGraph 类型收窄（不影响运行时行为，只为让 `build_graph` 体通过 mypy 检查）：
# 1. `build_report_guard_node`/`build_handoff_node` 的返回类型是 `Callable[...]` 别名，
#    mypy 无法把它匹配到 `add_node` 期望的节点协议；用 `StateNode` 显式标注。
# 2. `ROUTE_AFTER_*` 声明为 `dict[str, str]`，而 `add_conditional_edges` 的 `path_map`
#    形参是 `dict[Hashable, str]`，键类型不变，`dict[str, str]` 不能直接赋过去。
_Node = StateNode[ConsultationState, None]


def _path_map(mapping: Mapping[str, str]) -> dict[Hashable, str]:
    """把 `ROUTE_AFTER_*` 转成 `add_conditional_edges` 接受的 `dict[Hashable, str]`。"""
    return {key: value for key, value in mapping.items()}


async def intake(state: ConsultationState) -> dict:
    """受理：归属校验 + 会话装载 + 预算初始化 + 红线配置加载。

    受理即分流的落点：本节点之后的第一条边就是流程路由，不再有"先问症状"。
    `process_hint` 非空（小程序按钮直达）即锁定流程，跳过 triage 提问。
    """
    process_hint = state.get("process_hint")
    locked = process_hint is not None
    # 预算只在为空时初始化：恢复中断时 `intake` 可能重跑，若每次都重置为 initial，
    # 轮次上限会被"退出再进来"绕过，农户可无限追问（T2-32 必须堵住的漏洞）。
    budget = state.get("budget") or ConsultBudget.initial().to_state()
    return {
        "session_id": state.get("session_id"),
        "stage": ConsultStage.INTAKE,
        "budget": budget,
        "process_locked": locked,
        "process_id": ProcessId(process_hint) if locked else None,
        "redlines_loaded": state.get("redlines_loaded", False),  # 由 config 注入
    }


def confirm_object(state: ConsultationState) -> dict:
    """一次性对象确认：农户答复后回写 `process_id` 并锁定。

    未答复时只输出确认话术并置 `awaiting_object_confirm`；已答复则据其回写流程，
    并把"与档案冲突"单独置位——冲突只能回分诊重判（见 route_after_confirm_object）。
    """
    answer_process = state.get("confirm_object_answer")
    if answer_process is None:
        return {
            "reply": f"你问的是{state.get('object_label') or '哪一对象'}吗？确认一下我好对症下药。",
            "stage": ConsultStage.TRIAGE,
            "awaiting_object_confirm": True,
        }
    return {
        "process_id": ProcessId(answer_process),
        "process_locked": True,
        "object_confirmed_conflict": answer_process != state.get("profile_process_hint"),
        "stage": ConsultStage.TRIAGE,
    }


def subgraph_router(state: ConsultationState) -> dict:
    """纯路由节点：把控制交给选中子图，本身不改 state。"""
    return {}


def followup(state: ConsultationState) -> dict:
    """登记回访：处方卡通过门禁后调用回访服务，并投递 `prescription.created`。"""
    return {"stage": ConsultStage.FOLLOWUP}


def route_to_subgraph(state: ConsultationState) -> str:
    """按 `process_id` 选子图，未注册即抛错（不允许静默兜底）。

    静默兜底会把"新增流程忘了接线"藏到线上——农户拿到通用流程的处方。未登录流程
    一律抛 `UnroutedStateError`，让检查点回溯时能直接定位。
    """
    process_id = state.get("process_id")
    if process_id is None or process_id not in SUBGRAPH_BUILDERS:
        raise UnroutedStateError("route_to_subgraph", state)
    return f"subgraph_{process_id.value}"


async def build_graph() -> CompiledStateGraph:
    """构建并编译主图（含 Postgres checkpointer）。

    `async` 是必须的：编译期要 `await get_checkpointer()`。Postgres checkpointer 用
    连接池，不绑事件循环，跨循环复用同一个 saver 是安全的（T2-30）。
    """
    graph = StateGraph(ConsultationState)

    # 主图节点
    graph.add_node("intake", intake)
    graph.add_node("triage", run_triage)
    graph.add_node("confirm_object", confirm_object)
    graph.add_node("subgraph_router", subgraph_router)
    # config=None：按 state 取红线。工厂返回的是 `Callable` 别名，须标成节点协议才能过 mypy。
    graph.add_node("report_guard", cast(_Node, build_report_guard_node()))
    graph.add_node("followup", followup)
    graph.add_node("handoff", cast(_Node, build_handoff_node()))

    # 七条流程子图（子图内部自带 observe / report_guard / interview / verify / judge / prescribe）
    for process_id, builder in SUBGRAPH_BUILDERS.items():
        graph.add_node(f"subgraph_{process_id.value}", builder())

    from app.agent.graph.gate_graph import build_gate_subgraph  # 闸门子图

    graph.add_node("gate", build_gate_subgraph())

    # 入口：START -> intake（受理即分流从这里开始）
    graph.add_edge(START, "intake")

    # 第一条条件边：受理后立刻决定"判流程"还是"直达流程子图"
    graph.add_conditional_edges("intake", route_after_intake, _path_map(ROUTE_AFTER_INTAKE))

    # 分诊：判流程 / 对象确认 / 报告信号命中先过守卫 / 进流程子图
    graph.add_conditional_edges("triage", route_after_triage, _path_map(ROUTE_AFTER_TRIAGE))
    graph.add_conditional_edges("confirm_object", route_after_confirm_object, _path_map(ROUTE_AFTER_CONFIRM_OBJECT))
    graph.add_conditional_edges("report_guard", route_after_report_guard, _path_map(ROUTE_AFTER_REPORT_GUARD))

    # 子图分发：按 process_id 进对应子图；子图出口按守卫结论落到闸门或转人工
    graph.add_conditional_edges(
        "subgraph_router",
        route_to_subgraph,
        {f"subgraph_{p.value}": f"subgraph_{p.value}" for p in ProcessId},
    )
    for process_id in SUBGRAPH_BUILDERS:
        graph.add_conditional_edges(
            f"subgraph_{process_id.value}",
            route_after_subgraph,
            _path_map(ROUTE_AFTER_SUBGRAPH),
        )

    # 闸门出口：allow -> followup -> END；degrade/block -> handoff -> END
    graph.add_conditional_edges("gate", route_after_gate, _path_map(ROUTE_AFTER_GATE))
    graph.add_edge("followup", END)
    graph.add_edge("handoff", END)

    return graph.compile(checkpointer=await get_checkpointer())


__all__ = [
    "build_graph",
    "confirm_object",
    "followup",
    "intake",
    "route_to_subgraph",
    "subgraph_router",
]
