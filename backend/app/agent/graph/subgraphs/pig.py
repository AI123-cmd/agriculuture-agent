"""养猪流程子图：报告守卫前置，输出优先级最高（方案 7.4 唯一结构特例，T2-28）。

养猪以"不漏报"为最高目标，因此报告守卫插在 `observe` **之前**（入口守卫），其命中即出
子图、子图内部任何产出都不得覆盖；`observe` 之后再挂一次兜底守卫（方案 5.3.7 的旁路要求）。
两个守卫点共用 `build_report_guard_node` 与 `route_after_subgraph_guard`（按 `stage` 区分去向），
本项不新增路由函数、不改通用节点。

养猪 `panel_enabled=false`，故用剔除 `panel` 的面试路由映射。
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.agents.report_guard import build_report_guard_node
from app.agent.graph.factories import (
    get_config,
    make_interview_node,
    make_judge_node,
    make_observe_node,
    make_prescribe_node,
    make_verify_node,
)
from app.agent.graph.routes import (
    ROUTE_AFTER_INTERVIEW,
    ROUTE_AFTER_JUDGE,
    ROUTE_AFTER_SUBGRAPH_GUARD,
    route_after_interview,
    route_after_judge,
    route_after_subgraph_guard,
)
from app.agent.state import ConsultationState, ProcessId

# 无会诊流程的面试路由：从共用 path_map 里剔除 panel，保持同构且编译期不引用未注册节点。
_INTERVIEW_ROUTES = {key: target for key, target in ROUTE_AFTER_INTERVIEW.items() if key != "panel"}


def build_pig_subgraph() -> CompiledStateGraph:
    """养猪子图：入口守卫 → observe → 兜底守卫 → interview → verify → judge → prescribe。"""
    config = get_config(ProcessId.PIG)
    graph = StateGraph(ConsultationState)

    guard = build_report_guard_node(config)

    graph.add_node("report_guard_entry", guard)  # 入口守卫：先于 observe
    graph.add_node("observe", make_observe_node(config))
    graph.add_node("report_guard", guard)  # 兜底守卫：observe 之后
    graph.add_node("interview", make_interview_node(config))
    graph.add_node("verify", make_verify_node(config))
    graph.add_node("judge", make_judge_node(config))
    graph.add_node("prescribe", make_prescribe_node(config))

    # 入口守卫：命中即出子图（handoff 键在 ROUTE_AFTER_SUBGRAPH_GUARD 里映射 __end__）
    graph.add_edge(START, "report_guard_entry")
    graph.add_conditional_edges(
        "report_guard_entry",
        route_after_subgraph_guard,
        ROUTE_AFTER_SUBGRAPH_GUARD,
    )

    # observe 之后再做一次兜底检查（方案 5.3.7：observe 与 interview 之后各挂一次）
    graph.add_edge("observe", "report_guard")
    graph.add_conditional_edges("report_guard", route_after_subgraph_guard, ROUTE_AFTER_SUBGRAPH_GUARD)

    graph.add_conditional_edges("interview", route_after_interview, _INTERVIEW_ROUTES)
    graph.add_edge("verify", "judge")
    graph.add_conditional_edges("judge", route_after_judge, ROUTE_AFTER_JUDGE)
    graph.add_edge("prescribe", END)
    return graph.compile()
