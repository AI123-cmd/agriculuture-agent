"""置信度分级：规则化、确定性、可单测。

# TODO(T2-25): 本模块读的是旧编排状态的键（`image_url`、`symptoms`、`management`、
# `evidence_conflict`），这些键将由 `ConsultationState` 的 `slots` / `observations`
# 取代；评分改为按槽位覆盖度计算的动作在 T2-19，届时本模块随旧节点一并重写。
# 过渡期沿用 `TransitionalState` 作类型标注，理由见 `state.py` 中该类的说明。
"""

from app.agent.state import MANAGEMENT_KEYS, TransitionalState
from app.core.config import get_settings


def assess(state: TransitionalState) -> tuple[int, str]:
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
