"""杂草防治流程子图：结构与其他流程相同，差异全部来自 ProcessConfig（T2-27）。

杂草 `panel_enabled=true`，注册 `panel` 节点并保留 `ROUTE_AFTER_INTERVIEW` 的完整映射
（含 `panel` 分支）。
"""

from langgraph.graph import END, START, StateGraph

from app.agent.agents.report_guard import build_report_guard_node
from app.agent.graph.factories import (
    get_config,
    make_interview_node,
    make_judge_node,
    make_observe_node,
    make_panel_node,
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


def build_weed_subgraph():
    """杂草子图：observe → report_guard → interview →(panel)→ verify → judge → prescribe。"""
    config = get_config(ProcessId.WEED)
    graph = StateGraph(ConsultationState)

    graph.add_node("observe", make_observe_node(config))
    graph.add_node("report_guard", build_report_guard_node(config))
    graph.add_node("interview", make_interview_node(config))
    graph.add_node("verify", make_verify_node(config))
    graph.add_node("judge", make_judge_node(config))
    graph.add_node("prescribe", make_prescribe_node(config))

    panel = make_panel_node(config)
    if panel is not None:
        graph.add_node("panel", panel)

    graph.add_edge(START, "observe")
    graph.add_edge("observe", "report_guard")
    graph.add_conditional_edges("report_guard", route_after_subgraph_guard, ROUTE_AFTER_SUBGRAPH_GUARD)
    graph.add_conditional_edges("interview", route_after_interview, ROUTE_AFTER_INTERVIEW)
    if panel is not None:
        graph.add_edge("panel", "verify")
    graph.add_edge("verify", "judge")
    graph.add_conditional_edges("judge", route_after_judge, ROUTE_AFTER_JUDGE)
    graph.add_edge("prescribe", END)
    return graph.compile()
