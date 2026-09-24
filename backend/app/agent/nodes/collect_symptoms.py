"""症状采集节点：整理症状描述与照片证据。"""

from langchain_core.runnables import RunnableConfig

from app.agent.state import ConsultStage, TransitionalState


# TODO(T2-25): 标注用过渡状态（旧键清单），本节点由 T2-14 起替换为 `ConsultationState` 版本。
def collect_symptoms(state: TransitionalState, config: RunnableConfig | None = None) -> dict:
    symptoms = (state.get("symptoms") or "").strip()
    message = (state.get("user_message") or "").strip()
    image_url = state.get("image_url")

    # 首轮把用户描述当作症状输入（最佳努力提取），后续轮保留已采集内容
    if not symptoms and message:
        symptoms = message

    if not symptoms and not image_url:
        return {
            "symptoms": "",
            "stage": ConsultStage.OBSERVE.value,
            "reply": "请描述植株的异常症状，并尽量上传田间照片，便于我准确判断。",
            "related_suggestions": ["上传照片", "叶片发黄", "叶片斑点", "整体萎蔫"],
        }

    return {"symptoms": symptoms, "stage": ConsultStage.OBSERVE.value}
