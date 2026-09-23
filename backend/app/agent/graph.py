"""组装诊断状态机：节点 + 条件边 + 回环。"""
from langgraph.graph import END, START, StateGraph

from app.agent.checkpointer import get_checkpointer
from app.agent.nodes import (
    collect_management,
    collect_symptoms,
    diagnose,
    fetch_environment,
    intake,
    prescribe,
)
from app.agent.state import MANAGEMENT_KEYS, AgentState
from app.core.config import get_settings


def _route_after_symptoms(state: AgentState) -> str:
    if state.get("symptoms") or state.get("image_url"):
        return "collect_management"
    return "end"


def _route_after_management(state: AgentState) -> str:
    management = state.get("management") or {}
    if all(management.get(k) for k in MANAGEMENT_KEYS):
        return "fetch_environment"
    return "end"


def _route_after_diagnose(state: AgentState) -> str:
    settings = get_settings()
    if state.get("confidence_level") == "low" and state.get("round", 0) < settings.MAX_COLLECT_ROUNDS:
        return "collect_symptoms"
    return "prescribe"


async def build_graph():
    """构建并编译诊断图（含 SQLite checkpointer）。"""
    graph = StateGraph(AgentState)

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