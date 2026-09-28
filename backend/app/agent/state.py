"""全项目唯一的枚举来源，以及 LangGraph 编排状态定义（技术方案 5.3.1）。

枚举集中在一处（技术方案 5.2「枚举封闭」）是为了让契约层、智能体层、闸门层、
知识库层、模型网关引用同一份定义。若各处各写一遍字符串常量，一次措辞变更就会
在不同层之间产生不匹配，而这类不匹配发生在安全校验链路上是不可接受的。

本模块只放**纯定义**（枚举、`SlotValue`、`EvidenceItem`、`ConsultationState`），不放
任何写入函数：`app/process_config/models.py` 反向依赖本模块取枚举，若这里再导入流程
配置，就会形成 `state → process_config → state` 的循环导入。黑板读写函数因此单独放在
`app/agent/blackboard.py`。

全部枚举继承 `str, Enum`：枚举值会进入 YAML 配置、JSON 契约、JSONB 列与数据库
字符串列，`str` 混入让 `ProcessId.RICE == "rice"` 成立，序列化边界上无须反复做
类型转换，`pydantic` 的 `use_enum_values` 开关也不会改变行为。

自由文本落到枚举一律走 `parse_stage` / `parse_process_id`，解析失败即抛
`EnumParseError`（契约违约）。禁止用 `dict.get(..., default)` 静默兜底：「降级到
general」是一个显式的路由决策，必须由分诊智能体给出 `fallback_reason` 后才能发生。
"""

from datetime import date
from enum import Enum
from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, model_validator

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


class SlotValue(BaseModel):
    """单个信息槽位，带来源与状态（技术方案 5.3.1 原样定义）。

    `source` 必填，因为「没有来源的事实」在黑板上无法参与优先级比较；`status` 用
    `Literal` 而不是 `SlotStatus` 枚举，是为了与技术方案 5.3.1 的代码原文逐字一致，
    两者取值相同、序列化结果一致，不冲突。
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str | None = None
    status: Literal["filled", "unknown", "missing"] = "missing"
    source: Literal["profile", "sensor", "weather", "user_answer", "image", "inferred"]
    confidence: float | None = None
    asked_at_round: int | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "SlotValue":
        """拦最隐蔽的一类错误：置了值却忘改状态，或改了状态却没写值。

        这类不一致不会报任何异常，却直接导致追问筛选器把已答的槽位当成未问。
        `missing` 与 `unknown` 的差别是刻意保留的：前者是「还没问」，后者是
        「问过且农户答不出」，后者是合法答案，不得携带值。
        """
        if self.status == "filled" and self.value is None:
            raise ValueError("status=filled 时 value 不得为空")
        if self.status == "missing" and self.value is not None:
            raise ValueError("status=missing 时不得携带 value")
        return self


class EvidenceItem(BaseModel):
    """一条证据，必须带溯源（技术方案 5.3.1 原样定义）。

    `region_scope` 记录实际命中的行政级别（县/市/省/全国），这是地区回退时必须
    保留的信息，供界面标注「当前按 XX 市通用农时提示」。

    末尾七项是药剂条目专有的结构化字段，只有 `kind="pesticide"` 的条目有值。技术方案
    5.3.1 的原始定义只有前八个字段，这七项由 T2-21 补上：处方里的剂量区间与安全间隔期
    必须从知识库条目**照抄**（"模板出事实、模型只改写"），若证据条目不携带它们，处方
    就只能由模型生成数字，这条原则当场失效。可空是必须的——非药剂条目没有这些字段，
    而 `extra="forbid"` 不允许它们以未声明的方式出现。
    """

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str
    title: str
    content: str
    process_id: str
    region_scope: str | None
    effective_date: date | None
    kind: Literal["pest", "disease", "pesticide", "cultivation", "threshold"]
    score: float
    # 药剂条目专有：剂量区间、安全间隔期、每季使用上限、登记作物与禁忌搭配
    dosage_min: float | None = None
    dosage_max: float | None = None
    dosage_unit: str | None = None
    safety_interval_days: int | None = None
    max_uses_per_season: int | None = None
    registered_crops: list[str] = []
    forbidden_combinations: list[str] = []


class ConsultationState(TypedDict, total=False):
    """黑板式共享状态，同时是 LangGraph 的 State 类型（技术方案 5.3.1）。

    `total=False` 是关键：LangGraph 允许节点只返回自己改动的键，未返回的键保留
    上一轮的值。若用 `total=True`，每个节点都必须返回全部字段，实现会变得冗长
    且极易遗漏。

    事实类的键（`evidence`、`diagnosis`、`prescription`、`gate_result`、
    `report_decision`）都是 `dict` / `list[dict]` 而非契约对象：TypedDict 交给
    checkpointer 时需要可 JSON 序列化，契约对象在节点内部使用，入 state 前统一
    `model_dump(mode="json")`，出 state 时统一 `Model.model_validate(...)`。

    注意 LangGraph 的一个行为约束：节点内对 state 的**原地修改**
    （如 `state.setdefault("slots", {})[k] = v`）不会被持久化到后续节点，节点
    必须把改动**作为返回值**交给图层合并。`blackboard.write_slot` 因此既支持原地
    写入（供纯函数单测与节点工厂直接调用），也提供返回值供节点返回。
    """

    # 身份与归属
    session_id: str
    user_id: str
    farm_plot_id: str | None
    # 农户本次输入的原样文本与档案地区编码。技术方案 5.3.1 把它们放在 `messages` 与
    # 档案里，但报告守卫与转人工上下文包需要**不经模型**地直接读到原文与地区：守卫是
    # fail-closed 规则实现（T2-22），只能读 state，不能从消息列表里猜哪条是本次输入；
    # 地区编码由接口层注入（T2-33），用于解析联系入口（T2-22）与地区适宜性规则（T2-40）。
    user_message: str
    region_code: str | None
    # 流程
    process_id: ProcessId
    process_locked: bool
    # 入口直达提示（T2-25）：小程序按钮带来的 `process_hint`。非空说明入口已锁定流程，
    # intake 据此跳过 triage 提问；它是外部注入值，与分诊得出的 `process_id` 分开存放。
    process_hint: str | None
    # 流程红线是否已加载（T2-24）。路由函数首行检查它，为假即 fail-closed 转人工：
    # 红线配置缺失时用通用规则继续出方，等于突破产品说明书 9.3，属于不可接受的风险。
    redlines_loaded: bool
    # 流程红线快照（T2-45）。闸门不从流程配置重算红线，只消费 intake 装载进 state 的这份
    # `LoadedRedlines.model_dump()`；闸门是纯函数组件，读 state 之外无处取红线。
    redlines: dict
    # 本流程必填槽位键清单（T2-24）。`slots_ready` 只能做集合运算，不能查流程配置
    # ——路由函数必须是无 IO 的纯函数，故由 intake 把配置里的必填键摊平后写进 state。
    required_slots: list[str]
    # 会诊是否必要（T2-24）。由问诊节点按流程配置与该轮信息量置位，路由据此在
    # `verify` 与 `panel` 之间二选一，只读 bool，不对文本做关键词匹配。
    panel_needed: bool
    # config_version / config_hash 是技术方案 5.3.1 之外的必要推断：8.4 要求
    # `GateResult.checked_rules` 记录配置版本，而闸门只能从 state 读到它。
    config_version: int
    config_hash: str
    # 进展
    stage: ConsultStage
    round: int
    slot_round: int
    # 轮次与成本预算（T2-31）。存 `ConsultBudget.model_dump()` 的结果而非对象本身，
    # 与其余事实类键一致：checkpointer 需要可 JSON 序列化的值。路由函数直接按下标
    # 读 `state["budget"]["interview_rounds"]` 这类键，保持纯函数与可重放。
    budget: dict
    # token 超预算时置 True，网关据此路由到小模型档位；与轮次用尽（转人工）不同，
    # 这条不中断服务（T2-31 的第三种超预算处理）。
    degraded: bool
    # 档案输入（T2-14）。技术方案 5.3.1 的状态清单里没有这两项，但分诊要读档案里的
    # 经营条目与流程归属才能判"对象是否与档案相符"，且它必须来自调用方注入的只读快照
    # ——智能体层不得自己查库。
    profile: dict
    profile_process_id: str | None
    # 事实
    slots: dict[str, SlotValue]
    observations: dict  # 环境/图片归纳结果
    evidence: list[dict]  # EvidenceItem 序列化
    prescription_history: list[dict]  # 档案中的用药史，只读快照，与 slots 分开存放
    # 问诊产出（T2-26）。`pending_questions` 是本轮待农户回答的问题（`InterviewQuestion`
    # 序列化），`stage_hint` 是问诊智能体对下一步环节的建议。二者都由 `interview` 工厂节点
    # 返回，若不在此声明会被 LangGraph 静默丢弃，接口层随即拿不到问题时序。
    pending_questions: list[dict]
    stage_hint: str
    # 会诊产出（T2-26）。`panel_aggregate` 是聚合裁定结果（序列化），`panel_ran` 标记本次
    # 已会诊（防止回环重复消耗），`panel_force_uncertain` 供判定智能体在未收敛时强制输出
    # 不确定。三者同样必须声明，否则节点返回值会被丢弃。
    panel_aggregate: dict
    panel_ran: bool
    panel_force_uncertain: bool
    # 内部置信度分数（T2-26）。仅用于路由，不得进入任何展示文案（5.3.4 硬约束）。
    confidence_score: float
    # 对象确认交互（T2-25）。分诊判不出对象归属时插入一次确认，这三个键承载该交互：
    # `awaiting_object_confirm` 表示正等农户回答，`object_label` 是展示给农户的对象名，
    # `object_confirmed_conflict` 表示农户回答与档案记录冲突——冲突只能回分诊重判，
    # 不能就地改流程，否则档案一致性无从保证。
    awaiting_object_confirm: bool
    object_label: str
    object_confirmed_conflict: bool
    # 对象确认的农户答复（T2-25）。`confirm_object` 节点读它：非空则据此回写 `process_id`
    # 并锁定，为空则输出确认话术并置 `awaiting_object_confirm`。`profile_process_hint` 是
    # 档案记录的流程归属，用于判定"农户答复与档案冲突"，冲突只能回分诊重判。
    confirm_object_answer: str | None
    profile_process_hint: str | None
    # 产出
    # 分诊产出（T2-14）。存 `TriageResult` 序列化后的 dict，与其余事实类键一致。
    # 条件边 `route_after_triage`（T2-24）直接按下标读 `handoff_to_report` 与
    # `object_confirmed`：这两项决定主图走确认回环还是直接进流程子图，做成契约对象
    # 会让路由函数多一次校验，且校验失败时无法兜底（路由必须无 IO、无异常）。
    triage: dict
    diagnosis: dict | None
    prescription: dict | None
    # 处方卡最终展示文本（T2-45）。闸门的 `R-PROVENANCE`/`R-REDLINE`/`R-CAUTION` 反向扫描
    # 读的是它，闸门据此判断"模型说的"是否落在知识库与红线允许的范围内。`prescription_suppressed`
    # 在 degrade/block 时置真，展示层据此不渲染处方卡。
    prescription_text: str | None
    prescription_suppressed: bool
    gate_result: dict | None
    report_decision: dict | None
    # 转人工上下文包（T2-23）。存 `HandoffPackage.model_dump(mode="json")` 的结果，与其余
    # 事实类键一致。它必须在此声明：LangGraph **会静默丢弃**节点返回的未声明键，接口层
    # （T2-33）从这里读出并映射成响应里的 `handoff` 字段。
    handoff_package: dict | None
    # 会话
    messages: Annotated[list, add_messages]
    reply: str
    trace_id: str


__all__ = [
    "SOURCE_PRIORITY",
    "AnswerType",
    "ConfidenceLevel",
    "ConsultStage",
    "ConsultationState",
    "EvidenceItem",
    "EvidenceKind",
    "GateDecision",
    "ObjectType",
    "PanelResolution",
    "ProcessId",
    "SlotGroup",
    "SlotOwner",
    "SlotSource",
    "SlotStatus",
    "SlotValue",
    "TaskType",
    "TransitionalState",
    "TriggerSource",
    "ViolationSeverity",
    "parse_process_id",
    "parse_stage",
]
