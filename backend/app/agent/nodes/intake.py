"""接诊节点：确认作物与生长阶段。"""

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState, Step


def intake(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """接诊建档：作物未定由路由兜底到通用模型，阶段信息后续从档案注入。"""
    return {"step": Step.INTAKE.value}
