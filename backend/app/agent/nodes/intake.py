"""接诊节点：确认作物与生长阶段。"""

from langchain_core.runnables import RunnableConfig

from app.agent.state import ConsultStage, TransitionalState


def intake(state: TransitionalState, config: RunnableConfig | None = None) -> dict:
    """接诊建档：作物未定由路由兜底到通用模型，阶段信息后续从档案注入。

    返回值里的 `stage` 取枚举的 `.value`（纯字符串）而非枚举成员：状态会经
    checkpointer 序列化，纯字符串是唯一无需注册序列化器的形态。`str` 混入枚举
    让 `ConsultStage.INTAKE == "intake"` 成立，条件边与下游比较不受影响。

    # TODO(T2-25): 标注用过渡状态 `TransitionalState`（旧键清单），本节点由 T2-14
    # 起替换为 `ConsultationState` 版本后改回。
    """
    return {"stage": ConsultStage.INTAKE.value}
