"""管理追问节点：六问（施肥/打药/浇水/天气）证据采集。"""

from langchain_core.runnables import RunnableConfig

from app.agent.state import MANAGEMENT_KEYS, ConsultStage, TransitionalState

_FERTILIZE = ("施肥", "追肥", "尿素", "复合肥", "有机肥", "氮肥", "磷", "钾", "底肥", "叶面肥")
_PESTICIDE = ("打药", "农药", "喷药", "杀虫", "杀菌", "除草", "拌种", "蘸根")
_WATER = ("浇水", "灌溉", "灌水", "浇水", "排水", "渍水", "水田", "控水")
_WEATHER = ("下雨", "阴雨", "天气", "高温", "低温", "干旱", "台风", "霜冻", "降温")

_PROMPTS = {
    "fertilize": "近期是否施肥？施了什么肥、用量如何？",
    "pesticide": "近期是否打过药？用了什么药？",
    "water": "灌溉/浇水/排水情况如何？",
    "weather": "近期天气如何（是否高温、阴雨、干旱）？",
}


def _extract_management(message: str) -> dict:
    """规则化从自由文本抽取管理信息，供多轮增量累积。"""
    result: dict = {}
    if any(k in message for k in _FERTILIZE):
        result["fertilize"] = message
    if any(k in message for k in _PESTICIDE):
        result["pesticide"] = message
    if any(k in message for k in _WATER):
        result["water"] = message
    if any(k in message for k in _WEATHER):
        result["weather"] = message
    return result


# TODO(T2-25): 标注用过渡状态（旧键清单），本节点由 T2-15 起替换为 `ConsultationState` 版本。
def collect_management(state: TransitionalState, config: RunnableConfig | None = None) -> dict:
    management = dict(state.get("management") or {})
    message = (state.get("user_message") or "").strip()

    management.update(_extract_management(message))

    missing = [k for k in MANAGEMENT_KEYS if not management.get(k)]
    if missing:
        questions = "\n".join(f"· {_PROMPTS[k]}" for k in missing)
        return {
            "management": management,
            "stage": ConsultStage.INTERVIEW.value,
            "reply": "为准确诊断，请补充以下农事管理信息：\n" + questions,
            "related_suggestions": ["已施肥", "已打药", "正常灌溉", "近期阴雨"],
        }

    return {"management": management, "stage": ConsultStage.INTERVIEW.value}
