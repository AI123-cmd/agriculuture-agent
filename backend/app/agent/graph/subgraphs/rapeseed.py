"""油菜流程子图：占位实现，L3 处置知识检索关闭，禁止输出药剂推荐（T2-29）。

油菜专项调研未完成，按产品说明书 5.4 与附录 A7 以占位方式交付：不得预置油菜专属阈值与
药剂条目，不得输出药剂推荐。三条相互独立的约束同时生效——`rapeseed.yaml` 的 `RAPE-01`
红线由闸门拦截、L3 检索开关 `disabled` 让 `pesticide_items` 恒为空、输出定位为鉴别与农事。
子图本身不写死任何药剂名、剂量或安全间隔期，差异全部来自配置。

油菜 `panel_enabled=false`，故用剔除 `panel` 的面试路由映射。
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


def build_rapeseed_subgraph() -> CompiledStateGraph:
    """油菜子图：结构同构，处置知识与药剂输出由配置与闸门双重关闭。"""
    config = get_config(ProcessId.RAPESEED)
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
