"""转人工上下文包生成器（T2-23）。

四类必须转人工的情形（超出能力边界、安全拦截、连续未收敛、法定报告义务）在实现上是四条
不同的触发路径，但农户侧收到的应该是同一份东西：一份能带到农技站的"病历"——问题描述、
已确认信息、观察要点、已排除项，加上本地联系入口与一段可朗读的文本。

把它做成统一的 `HandoffPackage` 而不是四份各自定义的响应，是为了让接口层只有一个
`handoff` 字段类型（T2-33），前端渲染路径统一，避免"转人工"被当成异常在前端被吞掉。
转人工是**正常业务结果**，不是错误响应。

三处刻意不用模型：

- **字段内容**：已确认信息、观察要点、缺失项全部由代码从 state 与流程配置填充，模型无权
  改写。上下文包的作用是给农技人员看事实，掺入模型转述只会引入失真。
- **安全拦截的解释**（`_explain_block`）：只做原因归类，**不复述被拦截的具体方案**。产品
  说明书 6.2 要求校验不通过时"输出降级为建议联系当地农技站，而不是修改后输出"，若解释里
  带上"不建议使用某某药剂"就等于变相输出了被拦截的内容。
- **法定报告的强制动作**：优先取守卫给出的 `mandatory_actions`（守卫侧文案固定，T2-22），
  守卫未被调用时才落到本模块的兜底三条；并且该情形的 `excluded` 恒为空——技术方案 6.7
  明确疫情报告"不做责任或疫情的最终认定"。

`confirmed_facts` 的键走 `display_label` 而不是槽位 key，值再过一遍 `colloquial_map`：
农户要把这份信息读给农技人员听，`tea_last_pick` 这种标识没有意义。
"""

from enum import Enum
from typing import Any, Literal, Protocol

from langgraph.graph.state import StateNode
from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.agents.report_guard import ContactDirectory
from app.agent.contracts import ContactInfo, GateResult, ObservationResult, ReportDecision, dump_contract
from app.agent.state import ConsultationState, ConsultStage, ProcessId
from app.observability import log
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

# 转人工节点的形态：只吃 state，返回 state 增量。不带 `config` 参数是刻意的——LangGraph 会
# 把运行时 `RunnableConfig` 按形参名注入，而流程配置在节点构造时就已绑定。
# 用 LangGraph 节点协议标注，`StateGraph.add_node` 才能接受该工厂产出（裸 `Callable` 别名不匹配）。
HandoffNode = StateNode[ConsultationState, None]

# 法定报告情形在守卫未被调用时使用的兜底动作。写法与 T2-22 的守卫文案同源：先隔离、再消毒、
# 最后报告，顺序即展示顺序；文案里不出现任何疾病名称或"确诊"字样（不做疫情的最终认定）。
_FALLBACK_REPORT_ACTIONS: tuple[str, ...] = (
    "立即隔离发病猪只",
    "对栏舍消毒",
    "联系当地畜牧兽医部门报告",
)

# 超边界情形可自行完成的观察动作：不需要专业知识，农户照着做就能给农技人员提供有效信息。
_OUT_OF_SCOPE_OBSERVATIONS: tuple[str, ...] = (
    "拍几张清楚的照片，带一片有问题的叶子和整株的样子",
    "记下最近一次打药和施肥的时间",
    "留意症状是一两株还是整片都在发展",
)

# 超边界情形建议向农技人员问的问题。刻意是"怎么问"而不是"是什么病"：上下文包不承担
# 诊断职责，只帮农户把话问到位。
_OUT_OF_SCOPE_QUESTIONS: tuple[str, ...] = (
    "这种情况一般是什么原因引起的",
    "需不需要带样本过来做检测",
    "现在先做什么能防止继续发展",
)

# 安全拦截情形建议向农技人员问的问题。三条分别对应"能用什么药""有没有更安全的替代"
# "不打药还能做什么"，覆盖农户在拿不到用药建议时最实际的三条出路。
_SAFETY_BLOCK_QUESTIONS: tuple[str, ...] = (
    "我这种情况可以用什么药",
    "有没有更安全的替代办法",
    "如果不打药，还能做什么",
)

# 朗读文本里已确认信息与要点各取前几条。这段文本会被整段读完，长度必须可控；
# 上限属工程参数，可调整但需同步更新测试。
_VOICE_FACT_LIMIT = 5
_VOICE_ITEM_LIMIT = 5

# 问题描述的长度上限。上下文包是摘要而不是原始会话，超长原文对农技人员没有额外价值。
_SUMMARY_LIMIT = 200


class HandoffReason(str, Enum):
    """转人工原因。四值固定，与守卫的触发路径一一对应。"""

    OUT_OF_SCOPE = "out_of_scope"  # 超出能力边界
    SAFETY_BLOCK = "safety_block"  # 安全拦截（degrade 或 block）
    NOT_CONVERGED = "not_converged"  # 连续未收敛
    LEGAL_REPORT = "legal_report"  # 法定报告义务


class HandoffObservation(BaseModel):
    """一条观察要点。带来源，便于农技人员判断这条信息的可靠程度。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    source: Literal["user_answer", "sensor", "image", "diagnosis_basis"] = "user_answer"


class HandoffPackage(BaseModel):
    """转人工上下文包。农户可携带到农技站。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: HandoffReason
    headline: str  # 一句话说明为什么转人工
    problem_summary: str  # 问题描述
    confirmed_facts: dict[str, str] = {}  # 已确认信息，键为口语化标签
    observations: list[HandoffObservation] = []  # 观察要点
    excluded: list[str] = []  # 已排除项
    suggested_questions: list[str] = []  # 建议向农技人员问的问题
    contacts: list[ContactInfo] = []
    mandatory_actions: list[str] = []  # 法定报告情形的强制动作
    voice_text: str  # 供朗读的整段文本
    consult_summary_url: str | None = None  # 可分享的问诊摘要链接

    @model_validator(mode="after")
    def _legal_report_needs_actions(self) -> "HandoffPackage":
        """法定报告情形必须给出可执行动作。

        这一条是"法定报告视为正常分支而非兜底"的最小落地：农户拿到的不能是一句"需要报告"
        而没有"报什么、怎么报、先做什么"。
        """
        if self.reason is HandoffReason.LEGAL_REPORT and not self.mandatory_actions:
            raise ValueError("法定报告情形必须给出强制动作")
        return self

    @model_validator(mode="after")
    def _headline_not_blank(self) -> "HandoffPackage":
        """headline 是农户看到的第一句话，空串等于没有说明为什么转人工。"""
        if not self.headline.strip():
            raise ValueError("headline 不得为空")
        return self


class SummaryLinkBuilder(Protocol):
    """可分享问诊摘要链接的生成接口。实现在 T2-33 配套，本项只调用注入的实现。

    返回 `None` 是合法结果（摘要存储尚未接入），此时 `consult_summary_url` 留空，
    前端不渲染入口，而不是让整份上下文包构造失败。
    """

    def __call__(self, state: ConsultationState) -> str | None: ...


class _EmptyDirectory:
    """未接入联系入口时的占位实现：返回空列表，**绝不编造号码**。"""

    def for_region(self, region_code: str | None) -> list[ContactInfo]:
        return []


def resolve_handoff_reason(state: ConsultationState) -> HandoffReason:
    """按 state 上的产出判定转人工原因。纯函数，无 IO。

    判定顺序即优先级：法定报告最高（守卫命中即中止问诊，其余产出不得覆盖），其次是安全
    闸门的拦截结论，再次是判定的超边界标记，都不成立时才归结为"连续未收敛"——它是主图
    与子图在没有更明确原因时的出口。
    """
    decision = state.get("report_decision") or {}
    if decision.get("triggered") or decision.get("stop_consultation"):
        return HandoffReason.LEGAL_REPORT
    gate = state.get("gate_result") or {}
    if gate.get("decision") in ("degrade", "block"):
        return HandoffReason.SAFETY_BLOCK
    diagnosis = state.get("diagnosis") or {}
    if diagnosis.get("out_of_scope"):
        return HandoffReason.OUT_OF_SCOPE
    return HandoffReason.NOT_CONVERGED


def render_handoff_reply(package: HandoffPackage) -> str:
    """把上下文包渲染成面向农户的回复文本。固定模板，模型无权改写。

    两点与 `voice_text` 刻意不同：一是强制动作**不做条数截断**——法定报告情形的动作一条都
    不能少（T2-28 验收要求 `reply` 含 `mandatory_actions` 全部条目且顺序与契约一致）；
    二是联系入口单独成段，`phone` 为空时给出 `note` 里的查询口径而不是留白。
    """
    lines = [package.headline]
    if package.mandatory_actions:
        lines.extend(f"{index}. {action}" for index, action in enumerate(package.mandatory_actions, start=1))
    for observation in package.observations:
        lines.append(f"{observation.text}。")
    if package.suggested_questions:
        lines.append("可以问农技人员：" + "；".join(package.suggested_questions) + "。")
    if package.contacts:
        lines.append("联系方式：" + "；".join(_contact_line(contact) for contact in package.contacts) + "。")
    if package.consult_summary_url:
        lines.append(f"这份记录的链接：{package.consult_summary_url}")
    return "\n".join(lines)


class HandoffBuilder:
    """四种情形的统一构造入口。依赖两个注入点：联系入口与摘要链接。"""

    def __init__(self, contacts: ContactDirectory, summarizer: SummaryLinkBuilder | None = None) -> None:
        self._contacts = contacts
        self._summarizer = summarizer

    def build(
        self,
        reason: HandoffReason,
        state: ConsultationState,
        config: ProcessConfig,
        *,
        gate_result: GateResult | None = None,
        report_decision: ReportDecision | None = None,
        region_code: str | None = None,
    ) -> HandoffPackage:
        """按原因分派到四个构造构件。用 `match` 穷举并在末尾抛异常，新增情形时不会静默走某个分支。"""
        base = self._common_sections(state, config)
        match reason:
            case HandoffReason.OUT_OF_SCOPE:
                return self._out_of_scope(base, state, config, region_code)
            case HandoffReason.SAFETY_BLOCK:
                return self._safety_block(base, state, config, gate_result, region_code)
            case HandoffReason.NOT_CONVERGED:
                return self._not_converged(base, state, config, region_code)
            case HandoffReason.LEGAL_REPORT:
                return self._legal_report(base, state, config, report_decision, region_code)
        raise ValueError(f"未处理的转人工情形: {reason}")

    # ---- 四种情形 ----

    def _out_of_scope(
        self, base: dict[str, Any], state: ConsultationState, config: ProcessConfig, region_code: str | None
    ) -> HandoffPackage:
        """超能力边界：给"可自行观察的要点"与"建议问的问题"。

        这不是把农户推开：边界之外的问题本系统给不出准确建议，但可以告诉农户"看什么、问什么"，
        让他到农技站时手里有具体内容。判定给出的 `boundary_reason` 作为诊断依据保留在要点里。
        """
        headline = "这个问题超出我能给准建议的范围"
        observations = [HandoffObservation(text=text) for text in _OUT_OF_SCOPE_OBSERVATIONS]
        boundary_reason = (state.get("diagnosis") or {}).get("boundary_reason")
        if boundary_reason:
            observations.append(HandoffObservation(text=boundary_reason, source="diagnosis_basis"))
        return HandoffPackage(
            reason=HandoffReason.OUT_OF_SCOPE,
            headline=headline,
            problem_summary=self._problem_summary(state),
            confirmed_facts=base["confirmed_facts"],
            observations=observations,
            excluded=self._excluded_of(state),
            suggested_questions=list(_OUT_OF_SCOPE_QUESTIONS),
            contacts=self._contacts_for(region_code),
            mandatory_actions=[],
            voice_text=self._compose_voice(headline, base["confirmed_facts"], observations),
            consult_summary_url=self._summarize(state),
        )

    def _safety_block(
        self,
        base: dict[str, Any],
        state: ConsultationState,
        config: ProcessConfig,
        gate: GateResult | None,
        region_code: str | None,
    ) -> HandoffPackage:
        """安全拦截（degrade 或 block）：解释为什么不建议自行用药，不复述被拦截的方案。"""
        why = self._explain_block(gate)
        headline = "这个方案不建议自己动手，建议先问农技站"
        observations = [HandoffObservation(text=why, source="diagnosis_basis")]
        return HandoffPackage(
            reason=HandoffReason.SAFETY_BLOCK,
            headline=headline,
            problem_summary=self._problem_summary(state),
            confirmed_facts=base["confirmed_facts"],
            observations=observations,
            excluded=self._excluded_of(state),
            suggested_questions=list(_SAFETY_BLOCK_QUESTIONS),
            contacts=self._contacts_for(region_code),
            mandatory_actions=[],
            voice_text=self._compose_voice(headline, base["confirmed_facts"], observations),
            consult_summary_url=self._summarize(state),
        )

    def _not_converged(
        self, base: dict[str, Any], state: ConsultationState, config: ProcessConfig, region_code: str | None
    ) -> HandoffPackage:
        """连续未收敛：列"还缺这一项"而不是"我不知道"，让农户知道该补什么。

        观察要点用缺失项的 `display_label`，与 `confirmed_facts` 的键同一套口径，农户读起来
        连贯。
        """
        missing = self._missing_slots(state, config)
        headline = "问了这几轮还没能确定，建议带样本去农技站"
        observations = [HandoffObservation(text=f"还缺这一项：{label}") for label in missing]
        return HandoffPackage(
            reason=HandoffReason.NOT_CONVERGED,
            headline=headline,
            problem_summary=self._problem_summary(state),
            confirmed_facts=base["confirmed_facts"],
            observations=observations,
            excluded=self._excluded_of(state),
            suggested_questions=self._suggested_questions(missing),
            contacts=self._contacts_for(region_code),
            mandatory_actions=[],
            voice_text=self._compose_voice(headline, base["confirmed_facts"], observations),
            consult_summary_url=self._summarize(state),
        )

    def _legal_report(
        self,
        base: dict[str, Any],
        state: ConsultationState,
        config: ProcessConfig,
        decision: ReportDecision | None,
        region_code: str | None,
    ) -> HandoffPackage:
        """法定报告义务：强制动作 + 联系入口。

        动作优先取守卫给出的（文案固定、顺序即展示顺序），守卫未被调用时才用兜底三条，保证
        这条路径任何时候都产出可执行内容。`excluded` 恒为空是刻意的：技术方案 6.7 明确疫情
        报告"不做责任或疫情的最终认定"，列出已排除项等于变相给出了认定倾向。
        """
        actions = list(decision.mandatory_actions) if decision else list(_FALLBACK_REPORT_ACTIONS)
        contacts = list(decision.contacts) if decision and decision.contacts else self._contacts_for(region_code)
        headline = "需要马上报告，下面是该做的事"
        return HandoffPackage(
            reason=HandoffReason.LEGAL_REPORT,
            headline=headline,
            problem_summary=self._problem_summary(state),
            confirmed_facts=base["confirmed_facts"],
            observations=self._signal_observations(state),
            excluded=[],
            suggested_questions=[],
            contacts=contacts,
            mandatory_actions=actions,
            voice_text=self._compose_voice(headline, base["confirmed_facts"], actions),
            consult_summary_url=self._summarize(state),
        )

    # ---- 解释与要点 ----

    def _explain_block(self, gate: GateResult | None) -> str:
        """解释为什么不建议自行用药。只做原因归类，不得输出被拦截的具体方案。"""
        if gate is None:
            return "这个方案本身可能不安全，具体原因已记录，到农技站时可以直接说明本次咨询。"
        if not gate.process_redlines_loaded:
            return "这条流程的安全要求暂时没有完整加载，因此不能给用药建议。"
        fields = {v.field for v in gate.violations}
        if any("pesticide" in f for f in fields):
            return "推荐里的药剂不适合您这个作物或这个时期，不能照这个方案用药。"
        if any("interval" in f for f in fields):
            return "这个方案里的安全间隔期与您的采摘或出栏计划对不上，不能照这个方案用药。"
        return "这个方案没有通过安全校验，不能照这个方案用药。"

    def _signal_observations(self, state: ConsultationState) -> list[HandoffObservation]:
        """法定报告情形的观察要点：已采集的现场事实、图片候选，必要时回落农户原话。

        守卫在`observe`之前命中时现场数据尚未采集，此时把农户第一句描述作为要点——报告场景
        下这句话对农技人员最有价值，留空反而是信息损失。
        """
        items: list[HandoffObservation] = []
        raw = state.get("observations")
        if raw:
            result = ObservationResult.model_validate(raw)
            source: Literal["sensor", "user_answer"] = (
                "sensor" if result.env_source in ("sensor", "sensor_stale") else "user_answer"
            )
            items.extend(
                HandoffObservation(text=value, source=source) for value in result.observed_facts.values() if value
            )
            items.extend(
                HandoffObservation(text=f"照片看起来像{candidate.label}", source="image")
                for candidate in result.image_candidates
            )
        if not items and state.get("user_message"):
            items.append(HandoffObservation(text=state["user_message"][:_SUMMARY_LIMIT]))
        return items

    def _suggested_questions(self, missing: list[str]) -> list[str]:
        """按缺失项生成建议向农技人员问的问题，末尾补两条通用的。

        缺失项转成"这一项要怎么补"的问法，农户照着念就能把信息补齐；通用两条覆盖"要不要带
        样本"与"回去观察什么"——这两件事几乎每个转人工的农户都需要问。
        """
        questions = [f"{label}该怎么补上" for label in missing]
        questions.append("要不要带样本过来")
        questions.append("回去还要重点观察什么")
        return questions

    def _summarize(self, state: ConsultationState) -> str | None:
        """问诊摘要链接。由注入的 `SummaryLinkBuilder` 生成，未接入时留空。"""
        if self._summarizer is None:
            return None
        return self._summarizer(state)

    # ---- 公共字段 ----

    def _common_sections(self, state: ConsultationState, config: ProcessConfig) -> dict[str, Any]:
        """四种情形共用的字段：已确认信息的口语化呈现。

        跳过未填写与农户答"不清楚"的槽位：上下文包里列一条"这一项不清楚"对农技人员没有
        额外价值，缺失项由 `_not_converged` 单独列出。
        """
        confirmed: dict[str, str] = {}
        for key, slot in (state.get("slots") or {}).items():
            if slot.status != "filled" or not slot.value:
                continue
            if (spec := config.slot_spec(key)) is not None:
                confirmed[spec.display_label] = self._colloquialize(slot.value, config)
        return {"confirmed_facts": confirmed}

    def _colloquialize(self, text: str, config: ProcessConfig) -> str:
        """术语口语化替换。展示层与朗读文本都必须过这一步。"""
        for term, plain in config.terminology.colloquial_map.items():
            text = text.replace(term, plain)
        return text

    def _problem_summary(self, state: ConsultationState) -> str:
        """问题描述取农户本次输入的原话，缺失时回落到会话首条消息。"""
        first = (state.get("messages") or [{}])[0]
        return (state.get("user_message") or first.get("content") or "")[:_SUMMARY_LIMIT]

    def _excluded_of(self, state: ConsultationState) -> list[str]:
        """判定环节给出的已排除项。它是上下文包四项内容之一，对农技人员很有价值。"""
        return list((state.get("diagnosis") or {}).get("excluded") or [])

    def _missing_slots(self, state: ConsultationState, config: ProcessConfig) -> list[str]:
        """必问槽位中尚未确认的项，按 `ask_order` 排序返回口语化标签。

        口径与问诊的追问筛选器不同：这里只判"必问且还没问出来"，不跳过 `source_hint` 非空的
        槽位——自动来源（档案、传感器）在当前会话里没有填上，对农技人员而言就是缺口。
        """
        labels: list[str] = []
        for spec in sorted(config.required_slots, key=lambda s: s.ask_order):
            slot = (state.get("slots") or {}).get(spec.key)
            if spec.required and (slot is None or slot.status == "missing"):
                labels.append(spec.display_label)
        return labels

    def _compose_voice(
        self, headline: str, confirmed: dict[str, str], items: list[HandoffObservation] | list[str]
    ) -> str:
        """组装整段可朗读文本。句子短，避免书面长句（A5 假设：转人工引导文案配朗读按钮）。"""
        lines = [headline]
        if confirmed:
            facts = "，".join(f"{k}{v}" for k, v in list(confirmed.items())[:_VOICE_FACT_LIMIT])
            lines.append(f"已经了解到的情况是：{facts}。")
        for item in items[:_VOICE_ITEM_LIMIT]:
            lines.append(f"{item.text if isinstance(item, HandoffObservation) else item}。")
        return "".join(lines)

    def _contacts_for(self, region_code: str | None) -> list[ContactInfo]:
        """按地区解析联系入口。解析不到时留空并告警，绝不编造号码。"""
        try:
            return list(self._contacts.for_region(region_code))
        except Exception as exc:
            log.error(
                "handoff contacts lookup failed",
                extra={"region_code": region_code, "reason": str(exc)},
            )
            return []


def build_handoff_node(
    config: ProcessConfig | None = None,
    *,
    contacts: ContactDirectory | None = None,
    summarizer: SummaryLinkBuilder | None = None,
) -> HandoffNode:
    """构造转人工节点（主图与七条流程子图共用）。

    `config=None` 时节点按 state 上的 `process_id` 取流程配置（T2-25 的主图用法）；
    T2-26 的子图工厂传入该流程的配置，两者共用同一个节点函数——形态与
    `build_report_guard_node` 保持一致。

    节点写三样东西：`handoff_package`（上下文包，供接口层映射成 `handoff` 字段）、
    `reply`（农户可读文本，固定模板渲染）与 `stage`（置 `HANDOFF`，与 `BLOCKED` 分开统计）。
    """
    builder = HandoffBuilder(contacts if contacts is not None else _EmptyDirectory(), summarizer)

    async def handoff_node(state: ConsultationState) -> dict:
        process_config = config if config is not None else _config_for(state)
        package = builder.build(
            resolve_handoff_reason(state),
            state,
            process_config,
            gate_result=_gate_result_of(state),
            report_decision=_report_decision_of(state),
            region_code=state.get("region_code"),
        )
        return {
            "handoff_package": dump_contract(package),
            "reply": render_handoff_reply(package),
            "stage": ConsultStage.HANDOFF,
        }

    return handoff_node


def _gate_result_of(state: ConsultationState) -> GateResult | None:
    raw = state.get("gate_result")
    return GateResult.model_validate(raw) if raw else None


def _report_decision_of(state: ConsultationState) -> ReportDecision | None:
    raw = state.get("report_decision")
    return ReportDecision.model_validate(raw) if raw else None


def _config_for(state: ConsultationState) -> ProcessConfig:
    """按 state 上的 `process_id` 取流程配置；缺失时回落到通用流程。

    回落而不是抛异常：转人工是兜底路径，因为一份配置读不到就不给上下文包，等于把农户推回
    一个没有任何信息的对话结束。
    """
    raw = state.get("process_id")
    if isinstance(raw, ProcessId):
        return get_process_config(raw)
    try:
        return get_process_config(ProcessId(str(raw)))
    except ValueError:
        return get_process_config(ProcessId.GENERAL)


def _contact_line(contact: ContactInfo) -> str:
    """联系入口的一行文本。`phone` 为空时用 `note` 里的查询口径，不留白。"""
    detail = contact.phone or contact.note or "请通过当地农业农村部门查询"
    return f"{contact.name}（{detail}）"


__all__ = [
    "HandoffBuilder",
    "HandoffNode",
    "HandoffObservation",
    "HandoffPackage",
    "HandoffReason",
    "SummaryLinkBuilder",
    "build_handoff_node",
    "render_handoff_reply",
    "resolve_handoff_reason",
]
