"""LangGraph 诊断编排的状态定义。"""
from enum import Enum
from typing import Annotated, List, Optional, TypedDict

from langgraph.graph.message import add_messages


class Step(str, Enum):
    """诊断环节枚举，作为 checkpointer 持久化的进度标记。"""

    INTAKE = "intake"
    COLLECT_SYMPTOMS = "collect_symptoms"
    COLLECT_MANAGEMENT = "collect_management"
    FETCH_ENVIRONMENT = "fetch_environment"
    DIAGNOSE = "diagnose"
    PRESCRIBE = "prescribe"


# 六问（管理追问）的字段键，用于判断证据是否完整
MANAGEMENT_KEYS = ("fertilize", "pesticide", "water", "weather")


class AgentState(TypedDict, total=False):
    session_id: str
    user_id: str
    crop_tag: str
    step: str                       # 当前环节（checkpoint 持久化）
    round: int                      # 取证轮数（防死循环）
    image_url: Optional[str]
    user_message: str               # 本条用户输入（供节点读取）
    context: dict                   # 由 ai_service 构建的档案上下文
    symptoms: str
    management: dict                # 六问答案
    environment: dict               # 传感器/天气快照
    evidence_conflict: bool
    diagnosis: Optional[dict]
    confidence_score: float
    confidence_level: str           # high / medium / low
    messages: Annotated[List[dict], add_messages]  # 会话消息（短期记忆）
    reply: str
    related_suggestions: List[str]