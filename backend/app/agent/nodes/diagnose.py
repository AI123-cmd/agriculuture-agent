"""综合诊断节点：路由垂类模型 + 置信度分级 + 注入记忆。"""

from langchain_core.runnables import RunnableConfig

from app.agent.confidence import assess
from app.agent.nodes import _db_from_config
from app.agent.state import AgentState, Step
from app.ai_router.registry import resolve
from app.services.memory_service import MemoryService


async def diagnose(state: AgentState, config: RunnableConfig | None = None) -> dict:
    db = _db_from_config(config)
    crop_tag = state.get("crop_tag")
    symptoms = (state.get("symptoms") or "").strip()

    context = {
        **(state.get("context") or {}),
        "symptoms": symptoms,
        "management": state.get("management") or {},
        "environment": state.get("environment") or {},
    }

    # 注入中期（摘要）/长期（事实/画像）记忆
    if db is not None:
        context.update(await MemoryService().recall(db, state.get("user_id", ""), symptoms))

    model = resolve(crop_tag)
    text = await model.chat(symptoms, context)

    score, level = assess(state)
    result: dict = {
        "diagnosis": {"summary": text, "confidence": score, "level": level},
        "confidence_score": score,
        "confidence_level": level,
        "step": Step.DIAGNOSE.value,
    }
    # 低置信度时标记取证轮次，供条件路由决定是否回环补证据
    if level == "low":
        result["round"] = state.get("round", 0) + 1
    return result
