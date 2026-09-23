"""环境取证节点：调用 MCP 工具拉取传感器/天气/档案/知识库快照。"""

from langchain_core.runnables import RunnableConfig

from app.agent.nodes import _db_from_config
from app.agent.state import AgentState, Step
from app.core.config import get_settings
from app.tools import knowledge, profile, sensors, weather


async def fetch_environment(state: AgentState, config: RunnableConfig | None = None) -> dict:
    settings = get_settings()
    if not settings.MCP_ENABLED:
        return {"step": Step.FETCH_ENVIRONMENT.value, "environment": {}}

    db = _db_from_config(config)
    user_id = state.get("user_id", "")
    context = state.get("context") or {}
    location = (context.get("farmer") or {}).get("location", "")
    symptoms = (state.get("symptoms") or "").strip()

    environment = {
        "sensors": await sensors.query_sensors(db, user_id),
        "weather": weather.query_weather(location),
        "profile": await profile.query_profile(db, user_id),
    }
    if symptoms:
        environment["knowledge"] = knowledge.search_knowledge(symptoms)

    return {"step": Step.FETCH_ENVIRONMENT.value, "environment": environment}
