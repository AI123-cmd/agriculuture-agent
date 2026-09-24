"""全项目唯一的枚举来源，以及 LangGraph 编排状态定义。

枚举集中在一处（技术方案 5.2「枚举封闭」）是为了让契约层、智能体层、闸门层、
知识库层、模型网关引用同一份定义。若各处各写一遍字符串常量，一次措辞变更就会
在不同层之间产生不匹配，而这类不匹配发生在安全校验链路上是不可接受的。

全部枚举继承 `str, Enum`：枚举值会进入 YAML 配置、JSON 契约、JSONB 列与数据库
字符串列，`str` 混入让 `ProcessId.RICE == "rice"` 成立，序列化边界上无须反复做
类型转换，`pydantic` 的 `use_enum_values` 开关也不会改变行为。

自由文本落到枚举一律走 `parse_stage` / `parse_process_id`，解析失败即抛
`EnumParseError`（契约违约）。禁止用 `dict.get(..., default)` 静默兜底：「降级到
general」是一个显式的路由决策，必须由分诊智能体给出 `fallback_reason` 后才能发生。
"""

from enum import Enum
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages

from app.core.exceptions import EnumParseError


class ProcessId(str, Enum):
    """流程标识。七值固定，不接受动态注册。"""

    RICE = "rice"  # 水稻
    TEA = "tea"  # 茶
    RAPESEED = "rapeseed"  # 油菜
    CHILI = "chili"  # 辣椒
    WEED = "weed"  # 杂草防治，跨作物的问题类型，不绑定地块
    PIG = "pig"  # 养猪
    GENERAL = "general"  # 通用农业兜底


class ConsultStage(str, Enum):
    """问诊环节。INTAKE 至 PRESCRIBE 对齐六步，其后为安全与事件环节与两条终态。"""

    INTAKE = "intake"  # 受理，归属校验与会话装载
    TRIAGE = "triage"  # 判流程
    OBSERVE = "observe"  # 看现场
    INTERVIEW = "interview"  # 问五情
    VERIFY = "verify"  # 查依据
    JUDGE = "judge"  # 下判断
    PRESCRIBE = "prescribe"  # 开方
    GATE = "gate"  # 安全校验
    FOLLOWUP = "followup"  # 登记回访
    HANDOFF = "handoff"  # 转人工
    # BLOCKED 与 HANDOFF 必须分开：前者是闸门硬红线拒绝输出，后者是正常业务结果
    # （超边界、法定报告、连续未收敛），二者在审计表与线上指标上要能分开统计。
    BLOCKED = "blocked"  # 安全拦截


class SlotStatus(str, Enum):
    """槽位状态。UNKNOWN（问过且答不出）与 MISSING（还没问过）必须分开。"""

    FILLED = "filled"  # 已确认
    UNKNOWN = "unknown"  # 农户明确答不出，是合法答案
    MISSING = "missing"  # 尚未询问


class SlotSource(str, Enum):
    """槽位来源。数字越小优先级越高，见 `SOURCE_PRIORITY`。"""

    PROFILE = "profile"
    SENSOR = "sensor"
    WEATHER = "weather"
    USER_ANSWER = "user_answer"
    IMAGE = "image"
    INFERRED = "inferred"


# 数字越小优先级越高，高优先级可覆盖低优先级（技术方案 6.3）。
# `inferred` 永远无法覆盖 `profile`，这条由黑板写入函数按本表实现，不写进提示词。
SOURCE_PRIORITY: dict[SlotSource, int] = {
    SlotSource.PROFILE: 1,
    SlotSource.SENSOR: 2,
    SlotSource.WEATHER: 3,
    SlotSource.USER_ANSWER: 4,
    SlotSource.IMAGE: 5,
    SlotSource.INFERRED: 6,
}


class ConfidenceLevel(str, Enum):
    """把握程度。只允许三个值，对应三句农户能听懂的展示文案。

    数值型置信度分数仅用于内部路由，不得进入任何契约字段或展示文案（5.3.4 硬约束）。
    """

    LIKELY = "likely"  # 展示为"很可能是"
    PRELIMINARY = "preliminary"  # 展示为"初步判断"
    UNCERTAIN = "uncertain"  # 展示为"不确定"


class ViolationSeverity(str, Enum):
    """违规严重度。block 拦截输出，warn 仅告警放行。"""

    BLOCK = "block"
    WARN = "warn"


class GateDecision(str, Enum):
    """闸门裁定。degrade 表示降级输出（如去掉药剂条目）。"""

    ALLOW = "allow"
    DEGRADE = "degrade"
    BLOCK = "block"


class TriggerSource(str, Enum):
    """报告守卫的触发来源。"""

    SIGNAL = "signal"
    GROUP = "group"
    MASS_DEATH = "mass_death"
    NONE = "none"


class PanelResolution(str, Enum):
    """并行会诊的裁定方式。"""

    DECISIVE_FEATURE = "decisive_feature"
    ELIMINATION = "elimination"
    UNRESOLVED = "unresolved"


class SlotGroup(str, Enum):
    """槽位分类。用中文取值，因为它是配置里给人看的分类（技术方案 9.1）。"""

    SEEDING = "种情"
    FERTILIZER = "肥情"
    SOIL_MOISTURE = "墒情"
    SEEDLING = "苗情"
    PEST = "病虫情"
    MANAGEMENT = "管理"


class SlotOwner(str, Enum):
    """槽位唯一写入者。用英文，因为它是代码标识，出现在写入方的判断中。"""

    FIELD_OBSERVER = "field_observer"
    INTERVIEWER = "interviewer"
    PROFILE_LOADER = "profile_loader"


class EvidenceKind(str, Enum):
    """知识条目种类。"""

    PEST = "pest"
    DISEASE = "disease"
    PESTICIDE = "pesticide"
    CULTIVATION = "cultivation"
    THRESHOLD = "threshold"


class AnswerType(str, Enum):
    """追问的作答形态。"""

    SINGLE = "single"
    MULTI = "multi"
    TEXT = "text"
    PHOTO = "photo"
    NUMBER = "number"


class ObjectType(str, Enum):
    """诊断对象的类别（技术方案 12.2 档案模型）。"""

    CROP = "crop"
    LIVESTOCK = "livestock"
    ISSUE = "issue"


class TaskType(str, Enum):
    """模型网关的任务类型，每个值对应一个模型档位（技术方案 4.4）。"""

    TRIAGE = "triage"
    OBSERVE = "observe"
    INTERVIEW = "interview"
    RETRIEVE = "retrieve"
    DIAGNOSE = "diagnose"
    PRESCRIBE = "prescribe"
    PANEL = "panel"
    SNAPSHOT_WRITER = "snapshot_writer"
    REPORT_GUARD = "report_guard"


def parse_stage(raw: str) -> ConsultStage:
    """把外部传入的环节字符串解析为枚举，失败即抛异常触发降级。"""
    try:
        return ConsultStage(raw)
    except ValueError as exc:
        raise EnumParseError(f"非法 consult_stage: {raw!r}") from exc


def parse_process_id(raw: str) -> ProcessId:
    """流程标识解析，未登记的流程名不得静默回落到 general。"""
    try:
        return ProcessId(raw)
    except ValueError as exc:
        raise EnumParseError(f"非法 process_id: {raw!r}") from exc


# 六问（管理追问）的字段键，用于判断证据是否完整
MANAGEMENT_KEYS = ("fertilize", "pesticide", "water", "weather")


# TODO(T2-25): 过渡期保留的旧编排状态，由 T2-08 的 ConsultationState 取代，
# 主图在 T2-25 重建后本类随 graph.py / nodes/ 一并删除。
class AgentState(TypedDict, total=False):
    session_id: str
    user_id: str
    crop_tag: str
    stage: str  # 当前环节，取 ConsultStage 的值（checkpoint 持久化）
    round: int  # 取证轮数（防死循环）
    image_url: str | None
    user_message: str  # 本条用户输入（供节点读取）
    context: dict  # 由 ai_service 构建的档案上下文
    symptoms: str
    management: dict  # 六问答案
    environment: dict  # 传感器/天气快照
    evidence_conflict: bool
    diagnosis: dict | None
    confidence_score: float
    confidence_level: str  # high / medium / low
    messages: Annotated[list[dict], add_messages]  # 会话消息（短期记忆）
    reply: str
    related_suggestions: list[str]
