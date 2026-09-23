"""置信度分级：规则化、确定性、可单测。"""

from app.agent.state import MANAGEMENT_KEYS, AgentState
from app.core.config import get_settings


def assess(state: AgentState) -> tuple[int, str]:
    """根据证据信号计算置信度得分与分级。

    信号：图片 / 症状完整 / 管理完整 / 环境一致，各 1 分。
    """
    settings = get_settings()
    has_image = bool(state.get("image_url"))
    symptoms_complete = bool(state.get("symptoms"))
    management = state.get("management") or {}
    management_complete = all(k in management for k in MANAGEMENT_KEYS)
    environment_consistent = not state.get("evidence_conflict", False)

    score = sum(
        (
            1 if has_image else 0,
            1 if symptoms_complete else 0,
            1 if management_complete else 0,
            1 if environment_consistent else 0,
        )
    )

    if score >= settings.CONFIDENCE_HIGH_THRESHOLD and has_image:
        level = "high"
    elif score >= settings.CONFIDENCE_MEDIUM_THRESHOLD:
        level = "medium"
    else:
        level = "low"
    return score, level
