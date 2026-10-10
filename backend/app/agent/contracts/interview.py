"""问诊契约（技术方案 5.3.3）。

产品说明书 4.2.2 的五条关键设计规则在这里分成两类执行：能由字段结构约束的（每轮不超过
2 问、必须解释为什么问、选项与作答形态一致）由校验器直接执行；需要全局判断的（不重复问
已知项、剔除可自动读取的项）**不在这里做**——它们要读全量 `slots` 与流程配置，由 T2-17 的
候选池筛选器在调用模型之前完成。把过滤放进契约校验只会让模型先浪费一次调用生成已被过滤
的问题，再被丢弃。
"""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.process_config.models import MAX_OPTIONS_PER_BATCH, MAX_OPTIONS_PER_QUESTION

# 选项的展示文本。技术方案 5.3.3 把 `options` 定义为 `list[str]`，故此处只是概念别名而非
# 模型：改成 `{label, value}` 结构会偏离契约原文，并迫使前端跟着改协议。
QuestionOption: TypeAlias = str

AnswerTypeValue: TypeAlias = Literal["single", "multi", "text", "photo", "number"]

# 跳过项的判定词。单选/多选的选项列表必须固定包含其中一项，农户选它即写
# `SlotValue(status="unknown")`——「未知是合法答案」在选项层面的落地。
SKIP_MARKERS = frozenset({"说不清", "不清楚", "不知道", "跳过", "不好说"})

# 默认补位用的跳过项。取最常见的说法，农户不需要理解"跳过"这个界面用语。
_DEFAULT_SKIP_OPTION = "说不清"


def _cap_options(options: list[QuestionOption], limit: int) -> list[QuestionOption]:
    """把选项裁到 `limit` 项，并优先保住跳过项（T4-05）。

    先取前 `limit` 项；若其中没有跳过项，则用原列表里的跳过项替换掉第 `limit` 项。这样
    既保证"前若干项按原顺序保留"，又不会因为模型把跳过项排在末位就把它裁掉——裁掉跳过项
    等于剥夺农户"答不出"这个合法答案（产品说明书 4.2.2"未知是合法答案"）。
    """
    if len(options) <= limit:
        return list(options)
    kept = list(options[:limit])
    if any(opt in SKIP_MARKERS for opt in kept):
        return kept
    skip = next((opt for opt in options if opt in SKIP_MARKERS), None)
    if skip is None:
        return kept
    return [*options[: limit - 1], skip]


class InterviewQuestion(BaseModel):
    """单个追问。技术方案 5.3.3 原样定义。

    `why` 必填且非空，对应产品说明书 4.2.2 的"必须解释为什么问"。校验失败时的处置在
    T2-13 的基类里：重试一次，仍失败则降级为模板问题（模板里 `why` 已写好）。
    """

    model_config = ConfigDict(extra="forbid")

    slot_key: str
    text: str  # 面向农户的口语化问题
    why: str  # 为什么问这个（必填）
    options: list[QuestionOption] | None = None  # 快捷选项
    answer_type: AnswerTypeValue

    @field_validator("answer_type", mode="before")
    @classmethod
    def _accept_enum_value(cls, v: object) -> object:
        """接受 `AnswerType` 枚举成员，取其 `value` 后交给 `Literal` 校验。

        问诊模板里的 `answer_type` 来自配置的 `AnswerType` 枚举，而契约按技术方案原文
        标注为 `Literal`。在 `mode="before"` 里统一降为裸字符串，两处写法因此可以共存；
        取值是否合法仍由 `Literal` 判定，这里不做第二套白名单。
        """
        return getattr(v, "value", v)

    @field_validator("text", "why")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("不得为空")
        return v.strip()

    @field_validator("options", mode="before")
    @classmethod
    def _cap_question_options(cls, v: object) -> object:
        """单问选项裁到 `MAX_OPTIONS_PER_QUESTION`（T4-05），保留跳过项。"""
        if isinstance(v, list) and len(v) > MAX_OPTIONS_PER_QUESTION:
            return _cap_options(list(v), MAX_OPTIONS_PER_QUESTION)
        return v

    @model_validator(mode="after")
    def _options_match_answer_type(self) -> "InterviewQuestion":
        """拦"选了单选却没给选项"这类会让前端无法渲染的问题。"""
        if self.answer_type in ("single", "multi") and not self.options:
            raise ValueError("single/multi 类型必须给出选项")
        return self


class InterviewBatch(BaseModel):
    """一轮追问的完整输出。硬校验：1 至 2 问。"""

    model_config = ConfigDict(extra="forbid")

    # `max_length=2` 与下面的 `_max_two` 各管一半：前者让 JSON Schema 里出现
    # `maxItems: 2`（结构化输出约束要靠它，模型才会被硬限制在 2 问），后者按技术方案
    # 5.3.3 对照表"校验失败，裁剪至 2"把多出的问题**裁掉而不是报错**。裁剪必须发生在
    # 约束之前（`mode="before"`），否则 3 问会先撞上 `max_length` 直接抛错。
    questions: list[InterviewQuestion] = Field(min_length=1, max_length=2)
    stage_hint: str  # 进度提示文案，如"正在了解情况 2/3"
    confidence_note: str | None = None  # 数据缺失时的显式标注

    @field_validator("questions", mode="before")
    @classmethod
    def _max_two(cls, v: object) -> object:
        """多问一个问题的代价有限，不值得为此让整轮重试，因此裁剪而非抛错。"""
        if isinstance(v, list) and len(v) > 2:
            return v[:2]
        return v

    @model_validator(mode="after")
    def _at_least_one(self) -> "InterviewBatch":
        """零问题意味着契约违约（该智能体本该转其他节点），静默通过会让主图空转一轮。"""
        if not self.questions:
            raise ValueError("一轮至少一个问题，无问题时应进入下一环节")
        return self

    @model_validator(mode="after")
    def _no_duplicate_slot(self) -> "InterviewBatch":
        """同一轮问两次同一槽位是明显的生成异常，抛错而不是去重。"""
        keys = [q.slot_key for q in self.questions]
        if len(set(keys)) != len(keys):
            raise ValueError("同一轮不得重复询问同一槽位")
        return self

    @model_validator(mode="after")
    def _skip_option_present(self) -> "InterviewBatch":
        """缺跳过项时补位而不是抛错：漏加属于机械遗漏，补上即可，不影响语义。

        顺序是"原有选项不动、跳过项追加在末尾"：农户看到的选择顺序因此与模型给的
        顺序一致，避免同一问题的选项位置在不同轮次之间跳动。补位后仍受单问上限约束
        （T4-05 的 4 项含跳过项）——否则模型给满 4 项真实选项又漏了跳过项时会变成 5 项，
        既撑破版面，也会把一轮的选项总额度挤到第二问上。
        """
        for q in self.questions:
            if q.answer_type in ("single", "multi") and q.options:
                if not any(opt in SKIP_MARKERS for opt in q.options):
                    q.options = _cap_options([*q.options, _DEFAULT_SKIP_OPTION], MAX_OPTIONS_PER_QUESTION)
        return self

    @model_validator(mode="after")
    def _cap_batch_options(self) -> "InterviewBatch":
        """一轮的选项总数收敛到 `MAX_OPTIONS_PER_BATCH`（T4-05），保证首问完整。

        逐问先各裁到单问上限（`_cap_question_options` 已完成），再按总上限从第二问起依次
        收敛：首问信息量更大，优先保其选项完整；后续问题用剩余额度裁剪，同样保留跳过项。
        必须排在 `_skip_option_present` 之后——先补齐跳过项再算总数，否则会把补位挤掉。
        """
        used = 0
        for index, q in enumerate(self.questions):
            if not q.options:
                continue
            if index == 0:
                used = len(q.options)
                continue
            # 首问已被裁到单问上限 4，故剩余额至少为 `MAX_OPTIONS_PER_BATCH - 4`，恒为正，
            # 裁剪后仍至少留一项，不会出现"单选/多选没有选项"的非法状态。
            remaining = MAX_OPTIONS_PER_BATCH - used
            if len(q.options) > remaining:
                q.options = _cap_options(q.options, remaining)
            used += len(q.options)
        return self


__all__ = [
    "AnswerTypeValue",
    "InterviewBatch",
    "InterviewQuestion",
    "QuestionOption",
    "SKIP_MARKERS",
]
