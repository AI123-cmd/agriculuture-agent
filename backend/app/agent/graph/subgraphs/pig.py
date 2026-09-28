"""养猪流程子图：结构与其他流程同构，差异全部来自 ProcessConfig（T2-27）。

本项仅建立标准骨架（`observe → report_guard → interview → verify → judge → prescribe`），
**不**实现守卫前置插桩——守卫前置是结构特例，由 T2-28 处理。养猪 `panel_enabled=false`，
故用剔除 `panel` 的面试路由映射。
"""

from langgraph.graph import END, START, StateGraph

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


def build_pig_subgraph():
    """养猪子图标准骨架（守卫前置由 T2-28 插桩）。"""
    config = get_config(ProcessId.PIG)
    graph = StateGraph(ConsultationState)

    graph.add_node("observe", make_observe_node(config))
    graph.add_node("report_guard", build_report_guard_node(config))
    graph.add_node("interview", make_interview_node(config))
    graph.add_node("verify", make_verify_node(config))
    graph.add_node("judge", make_judge_node(config))
    graph.add_node("prescribe", make_prescribe_node(config))

    graph.add_edge(START, "observe")
    graph.add_edge("observe", "report_guard")
    graph.add_conditional_edges("report_guard", route_after_subgraph_guard, ROUTE_AFTER_SUBGRAPH_GUARD)
    graph.add_conditional_edges("interview", route_after_interview, _INTERVIEW_ROUTES)
    graph.add_edge("verify", "judge")
    graph.add_conditional_edges("judge", route_after_judge, ROUTE_AFTER_JUDGE)
    graph.add_edge("prescribe", END)
    return graph.compile()
