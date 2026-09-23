"""开处方节点：结构化处方序列化为纯文本 + 安全用药规范。"""
from app.agent.state import AgentState, Step


def prescribe(state: AgentState, config=None) -> dict:
    diagnosis = state.get("diagnosis") or {}
    summary = diagnosis.get("summary") or ""
    level = state.get("confidence_level", "medium")

    parts = ["【诊断结果】", summary or "（暂无法给出明确结论）"]

    if level == "low":
        parts.append(
            "【提示】当前信息有限，以上为保守建议，建议补充田间照片或现场咨询农技人员。"
        )

    parts.append(
        "【用药提醒】请轮换用药，单季同类药剂不超过 2 次；"
        "具体用药请结合当地植保部门指导，本建议仅供参考。"
    )

    return {
        "reply": "\n\n".join(parts),
        "related_suggestions": ["3天后复查", "追加田间照片", "联系农技员"],
        "step": Step.PRESCRIBE.value,
    }