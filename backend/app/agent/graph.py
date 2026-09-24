"""组装诊断状态机：节点 + 条件边 + 回环。

# TODO(T2-25): 本模块是过渡期的旧线性图（节点名仍为 `collect_symptoms` 等），
# 由 T2-25 的 `graph/main.py` 取代后整体删除。

图的 state 类型取 `TransitionalState` 而非 `ConsultationState`：LangGraph **会静默
丢弃**节点返回的、未在 state 类型中声明的键，而现有节点（T2-14 起才逐个替换）仍
返回 `symptoms`、`management`、`confidence_level` 等旧键。换成新状态会同时导致
mypy 报错与条件边静默全部走到 `end`。
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.checkpointer import get_checkpointer
from app.agent.nodes import (
    collect_management,
    collect_symptoms,
    diagnose,
    fetch_environment,
    intake,
    prescribe,
)
from app.agent.state import MANAGEMENT_KEYS, TransitionalState
from app.core.config import get_settings


def _route_after_symptoms(state: TransitionalState) -> str:
    if state.get("symptoms") or state.get("image_url"):
        return "collect_management"
    return "end"


def _route_after_management(state: TransitionalState) -> str:
    management = state.get("management") or {}
    if all(management.get(k) for k in MANAGEMENT_KEYS):
        return "fetch_environment"
    return "end"


def _route_after_diagnose(state: TransitionalState) -> str:
    settings = get_settings()
    if state.get("confidence_level") == "low" and state.get("round", 0) < settings.MAX_COLLECT_ROUNDS:
        return "collect_symptoms"
    return "prescribe"


async def build_graph() -> CompiledStateGraph:
    """构建并编译诊断图（含 PostgreSQL checkpointer）。"""
    graph = StateGraph(TransitionalState)

    graph.add_node("intake", intake)
    graph.add_node("collect_symptoms", collect_symptoms)
    graph.add_node("collect_management", collect_management)
    graph.add_node("fetch_environment", fetch_environment)
    graph.add_node("diagnose", diagnose)
    graph.add_node("prescribe", prescribe)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "collect_symptoms")

    graph.add_conditional_edges(
        "collect_symptoms",
        _route_after_symptoms,
        {"collect_management": "collect_management", "end": END},
    )
    graph.add_conditional_edges(
        "collect_management",
        _route_after_management,
        {"fetch_environment": "fetch_environment", "end": END},
    )

    graph.add_edge("fetch_environment", "diagnose")
    graph.add_conditional_edges(
        "diagnose",
        _route_after_diagnose,
        {"collect_symptoms": "collect_symptoms", "prescribe": "prescribe"},
    )
    graph.add_edge("prescribe", END)

    return graph.compile(checkpointer=await get_checkpointer())
