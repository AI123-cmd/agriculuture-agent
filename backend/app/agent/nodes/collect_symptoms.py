"""症状采集节点：整理症状描述与照片证据。"""
from app.agent.state import AgentState, Step


def collect_symptoms(state: AgentState, config=None) -> dict:
    symptoms = (state.get("symptoms") or "").strip()
    message = (state.get("user_message") or "").strip()
    image_url = state.get("image_url")

    # 首轮把用户描述当作症状输入（最佳努力提取），后续轮保留已采集内容
    if not symptoms and message:
        symptoms = message

    if not symptoms and not image_url:
        return {
            "symptoms": "",
            "step": Step.COLLECT_SYMPTOMS.value,
            "reply": "请描述植株的异常症状，并尽量上传田间照片，便于我准确判断。",
            "related_suggestions": ["上传照片", "叶片发黄", "叶片斑点", "整体萎蔫"],
        }

    return {"symptoms": symptoms, "step": Step.COLLECT_SYMPTOMS.value}