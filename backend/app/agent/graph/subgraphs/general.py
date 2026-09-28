"""通用农业流程子图：兜底定位，能给出可用通用建议但不承担专属判断（T2-29）。

通用流程是分诊降级的目标（`TriageResult.fallback_reason` 非空时落到此处）：`calendar_source`
为 `None`（不建农事历）、无专属阈值与药剂条目、`panel_enabled=false`。子图本身与其余流程
同构，差异全部来自 `general.yaml`，故此处不写死任何专属内容。

通用 `panel_enabled=false`，故用剔除 `panel` 的面试路由映射。
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


def build_general_subgraph():
    """通用子图：结构同构，无专属知识、无农事历、无药剂条目。"""
    config = get_config(ProcessId.GENERAL)
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
