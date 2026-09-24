"""T2-23：转人工上下文包的四类情形、字段口径与朗读文本。

用例分六组：

1. **四类情形**：超边界含可自行观察的要点与判定给出的边界原因；安全拦截只解释原因；未收敛列
   缺失项；法定报告带强制动作与联系入口；
2. **契约校验**：`LEGAL_REPORT` 无强制动作、`headline` 为空各抛 `ValidationError`；
3. **不复述被拦截的方案**：构造带药剂名的闸门违规，断言它不出现在 `observations` 与
   `suggested_questions` 里——这是产品说明书 6.2"降级而不是改写后输出"的落地；
4. **字段口径**：`confirmed_facts` 的键全部来自 `display_label`、值过 `colloquial_map`；
5. **朗读文本**：由 `headline` 开头、要点不超过五条；四种情形都产出非空内容；
6. **节点工厂**：`build_handoff_node` 的无参形态（T2-25 主图）与传 config 形态（T2-26 子图）
   都成立，且节点只写声明的三个键。
"""

import inspect
from typing import Any, cast

import pytest
from pydantic import ValidationError

import app.agent.agents.handoff as handoff_module
from app.agent.agents.handoff import (
    HandoffBuilder,
    HandoffObservation,
    HandoffPackage,
    HandoffReason,
    build_handoff_node,
    render_handoff_reply,
    resolve_handoff_reason,
)
from app.agent.agents.report_guard import ContactDirectory
from app.agent.contracts import ContactInfo, GateResult, GateViolation, ReportDecision
from app.agent.state import ConsultStage, ConsultationState, ProcessId, SlotValue
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_REGION_CODE = "330102"

# 被拦截方案里的药剂名。安全拦截的解释里绝不能出现它：出现即等于变相输出了被拦截的内容。
_BLOCKED_PESTICIDE = "吡虫啉"


class _FakeContacts:
    """联系入口的替身：按地区回放入口，或整体抛一个异常。"""

    def __init__(
        self, by_region: dict[str, list[ContactInfo]] | None = None, raise_exc: Exception | None = None
    ) -> None:
        self._by_region = by_region or {}
        self._raise = raise_exc
        self.codes: list[str | None] = []

    def for_region(self, region_code: str | None) -> list[ContactInfo]:
        self.codes.append(region_code)
        if self._raise is not None:
            raise self._raise
        return list(self._by_region.get(region_code or "", []))


class _LogRecorder:
    """记录结构化日志事件的替身，用于断言"联系入口解析不到时记 ERROR"。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def errors(self) -> list[tuple[str, dict[str, Any]]]:
        return [e for e in self.events if e[0].startswith("handoff")]


def _tea_station() -> ContactInfo:
    return ContactInfo(kind="agri_station", name="XX 县农技站", phone="0571-87654321", region_code=_REGION_CODE)


def _vet_station() -> ContactInfo:
    return ContactInfo(kind="vet_station", name="XX 县畜牧兽医站", phone="0571-12345678", region_code=_REGION_CODE)


@pytest.fixture()
def logs(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(handoff_module, "log", recorder)
    return recorder


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(ProcessId.TEA)


@pytest.fixture(scope="module")
def pig_config() -> ProcessConfig:
    return get_process_config(ProcessId.PIG)


def _slot(key: str, value: str | None, status: str = "filled") -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source="user_answer")  # type: ignore[arg-type]


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "process_id": ProcessId.TEA,
        "round": 5,
        "slot_round": 4,
        "user_message": "我家茶树叶子发黄，还有斑，打了两回药也没好",
        "messages": [{"role": "user", "content": "我家茶树叶子发黄，还有斑"}],
    }
    state.update(overrides)
    return state


def _builder(contacts: ContactDirectory | None = None, summarizer: Any = None) -> HandoffBuilder:
    return HandoffBuilder(contacts if contacts is not None else _FakeContacts(), summarizer)


def _gate(
    *,
    decision: str = "block",
    process_redlines_loaded: bool = True,
    violations: list[GateViolation] | None = None,
) -> GateResult:
    items = violations if violations is not None else []
    return GateResult(
        passed=False,
        violations=items,
        checked_rules=["R-REDLINE"],
        process_redlines_loaded=process_redlines_loaded,
        decision=decision,  # type: ignore[arg-type]
        degrade_output="校验未通过，建议联系当地农技站" if decision == "degrade" else None,
    )


def _pesticide_violation() -> GateViolation:
    """药剂违规的 `field` 取真实规则（T2-36）的写法：`pesticide_items[<knowledge_id>]`。

    违规的 `message` 里带药剂名（规则原文如此），而 `field` 只带知识条目 id。`_explain_block`
    只读 `field` 做归类，因此药名不会经由解释文案泄漏。
    """
    return GateViolation(
        rule_id="R-REGISTERED-CROP",
        severity="block",
        message=f"药剂《{_BLOCKED_PESTICIDE}》未在茶树上登记（登记作物：水稻、小麦）",
        field="pesticide_items[K-1023]",
    )


def _interval_violation() -> GateViolation:
    """间隔期违规的 `field` 取真实规则（T2-38）的写法：`safety_interval_days`。"""
    return GateViolation(
        rule_id="R-INTERVAL",
        severity="block",
        message="安全间隔期与采摘计划冲突",
        field="safety_interval_days",
    )


def _report_decision(actions: list[str] | None = None, contacts: list[ContactInfo] | None = None) -> ReportDecision:
    return ReportDecision(
        triggered=True,
        matched_signals=["sudden_death"],
        trigger_source="signal",
        mandatory_actions=actions if actions is not None else ["立即隔离发病猪只", "对栏舍消毒"],
        contacts=contacts if contacts is not None else [],
        stop_consultation=True,
    )


# ---- 第一组：四类情形 ----


def test_out_of_scope_has_observable_points(tea_config: ProcessConfig) -> None:
    """超边界给"可自行观察的要点"与判定给出的边界原因，不是一句无解释的拒答。"""
    state = _state(diagnosis={"out_of_scope": True, "boundary_reason": "超出茶树病虫害范围", "excluded": ["缺素"]})
    package = _builder().build(HandoffReason.OUT_OF_SCOPE, state, tea_config)

    assert package.reason is HandoffReason.OUT_OF_SCOPE
    assert package.headline == "这个问题超出我能给准建议的范围"
    assert package.observations
    assert any(item.source == "diagnosis_basis" and "超出茶树病虫害范围" in item.text for item in package.observations)
    assert package.suggested_questions
    assert package.excluded == ["缺素"]
    assert package.mandatory_actions == []


def test_safety_block_explains_without_restating(tea_config: ProcessConfig) -> None:
    """安全拦截只解释原因，observation 的来源是诊断依据。"""
    state = _state(gate_result=_gate(violations=[_pesticide_violation()]).model_dump(mode="json"))
    package = _builder().build(
        HandoffReason.SAFETY_BLOCK, state, tea_config, gate_result=_gate(violations=[_pesticide_violation()])
    )

    assert package.headline == "这个方案不建议自己动手，建议先问农技站"
    assert len(package.observations) == 1
    assert package.observations[0].source == "diagnosis_basis"
    assert package.suggested_questions == ["我这种情况可以用什么药", "有没有更安全的替代办法", "如果不打药，还能做什么"]


def test_not_converged_lists_missing_slots(tea_config: ProcessConfig) -> None:
    """未收敛列"还缺这一项"而不是"我不知道"，让农户知道该补什么。"""
    state = _state(slots={"tea_symptom_part": _slot("tea_symptom_part", "叶片")})
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert package.headline == "问了这几轮还没能确定，建议带样本去农技站"
    texts = [item.text for item in package.observations]
    # tea.yaml 中 required=true 的槽位共五项，已填一项，其余四项应逐个列出
    assert len(texts) == 4
    assert all(text.startswith("还缺这一项：") for text in texts)
    # ask_order 排序：tea_symptom_pattern(20) 在 tea_last_pick(50) 之前
    assert texts.index("还缺这一项：是什么样子的") < texts.index("还缺这一项：上次采摘是哪天")


def test_not_converged_asks_followup_questions(tea_config: ProcessConfig) -> None:
    """建议问题由缺失项生成，末尾补两条通用问题。"""
    state = _state(slots={})
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert any("该怎么补上" in question for question in package.suggested_questions)
    assert package.suggested_questions[-2:] == ["要不要带样本过来", "回去还要重点观察什么"]


def test_legal_report_carries_actions_and_contacts(pig_config: ProcessConfig) -> None:
    """法定报告带强制动作与联系入口，且已排除项恒为空（不做疫情的最终认定）。"""
    state = _state(
        process_id=ProcessId.PIG,
        report_decision=_report_decision(contacts=[_vet_station()]).model_dump(mode="json"),
        diagnosis={"excluded": ["猪瘟"]},
    )
    package = _builder(_FakeContacts({_REGION_CODE: [_tea_station()]})).build(
        HandoffReason.LEGAL_REPORT, state, pig_config, report_decision=_report_decision(contacts=[_vet_station()])
    )

    assert package.headline == "需要马上报告，下面是该做的事"
    assert package.mandatory_actions == ["立即隔离发病猪只", "对栏舍消毒"]
    assert package.contacts and package.contacts[0].kind == "vet_station"
    assert package.excluded == []
    assert package.suggested_questions == []


def test_legal_report_falls_back_to_directory(pig_config: ProcessConfig) -> None:
    """守卫未给联系入口时按地区解析联系目录，而不是留空。"""
    contacts = _FakeContacts({_REGION_CODE: [_vet_station()]})
    state = _state(process_id=ProcessId.PIG, region_code=_REGION_CODE)
    package = _builder(contacts).build(
        HandoffReason.LEGAL_REPORT, state, pig_config, report_decision=_report_decision(), region_code=_REGION_CODE
    )

    assert package.contacts == [_vet_station()]
    assert contacts.codes == [_REGION_CODE]


def test_legal_report_keeps_guard_contacts(pig_config: ProcessConfig) -> None:
    """守卫已给出联系入口时不再查目录：守卫的口径优先，避免两处各给一个号码。"""
    contacts = _FakeContacts({_REGION_CODE: [_tea_station()]})
    state = _state(process_id=ProcessId.PIG, region_code=_REGION_CODE)
    package = _builder(contacts).build(
        HandoffReason.LEGAL_REPORT,
        state,
        pig_config,
        report_decision=_report_decision(contacts=[_vet_station()]),
        region_code=_REGION_CODE,
    )

    assert package.contacts == [_vet_station()]
    assert contacts.codes == []


def test_legal_report_without_decision_uses_fallback_actions(pig_config: ProcessConfig) -> None:
    """守卫未被调用时用兜底三条，保证这条路径任何时候都产出可执行内容。"""
    package = _builder().build(HandoffReason.LEGAL_REPORT, _state(process_id=ProcessId.PIG), pig_config)

    assert package.mandatory_actions == ["立即隔离发病猪只", "对栏舍消毒", "联系当地畜牧兽医部门报告"]


def test_out_of_scope_observations_from_signal_fallback(pig_config: ProcessConfig) -> None:
    """守卫在 observe 之前命中时现场数据尚未采集，要点回落到农户原话，而不是留空。"""
    state = _state(process_id=ProcessId.PIG, user_message="昨天死了一头，早上还好好的")
    package = _builder().build(HandoffReason.LEGAL_REPORT, state, pig_config, report_decision=_report_decision())

    assert package.observations
    assert "昨天死了一头" in package.observations[0].text


def test_signal_observations_prefer_collected_facts(pig_config: ProcessConfig) -> None:
    """已有现场事实时优先用它，并对图片候选单独标注来源。"""
    state = _state(
        process_id=ProcessId.PIG,
        observations={
            "data_available": True,
            "env_source": "sensor",
            "observed_facts": {"体温": "41.5 度"},
            "image_candidates": [{"label": "皮肤发绀", "score": 0.8}],
        },
    )
    package = _builder().build(HandoffReason.LEGAL_REPORT, state, pig_config, report_decision=_report_decision())

    sources = {item.source for item in package.observations}
    assert "sensor" in sources and "image" in sources
    assert any("41.5 度" in item.text for item in package.observations)


# ---- 第二组：契约校验 ----


def test_legal_report_without_actions_raises() -> None:
    """法定报告情形缺强制动作时构造失败——这正是"不是兜底而是正常分支"的最小落地。"""
    with pytest.raises(ValidationError):
        HandoffPackage(reason=HandoffReason.LEGAL_REPORT, headline="需要马上报告", problem_summary="", voice_text="x")


def test_blank_headline_raises() -> None:
    """headline 是农户看到的第一句话，空串等于没有说明为什么转人工。"""
    for blank in ("", "   "):
        with pytest.raises(ValidationError):
            HandoffPackage(
                reason=HandoffReason.NOT_CONVERGED,
                headline=blank,
                problem_summary="",
                voice_text="x",
            )


def test_non_legal_reason_allows_empty_actions() -> None:
    """其余三类情形不要求强制动作：校验只在法定报告情形生效。"""
    package = HandoffPackage(
        reason=HandoffReason.NOT_CONVERGED, headline="问了这几轮还没能确定", problem_summary="", voice_text="x"
    )

    assert package.mandatory_actions == []


# ---- 第三组：不复述被拦截的方案 ----


def test_safety_block_hides_blocked_pesticide(tea_config: ProcessConfig) -> None:
    """闸门违规里的药剂名不得出现在上下文包里：出现即等于变相输出了被拦截的内容。"""
    gate = _gate(violations=[_pesticide_violation()])
    state = _state(gate_result=gate.model_dump(mode="json"))
    package = _builder().build(HandoffReason.SAFETY_BLOCK, state, tea_config, gate_result=gate)

    blob = " ".join(
        [package.headline, package.problem_summary, package.voice_text, *package.suggested_questions]
        + [item.text for item in package.observations]
    )
    assert _BLOCKED_PESTICIDE not in blob


def test_safety_block_explains_pesticide_scope(tea_config: ProcessConfig) -> None:
    """药剂类违规归到"药剂不适合您这个作物或这个时期"。"""
    gate = _gate(violations=[_pesticide_violation()])
    package = _builder().build(HandoffReason.SAFETY_BLOCK, _state(), tea_config, gate_result=gate)

    assert "药剂不适合您这个作物或这个时期" in package.observations[0].text


def test_safety_block_explains_interval_conflict(tea_config: ProcessConfig) -> None:
    """间隔期类违规归到"安全间隔期与采摘或出栏计划对不上"。"""
    gate = _gate(violations=[_interval_violation()])
    package = _builder().build(HandoffReason.SAFETY_BLOCK, _state(), tea_config, gate_result=gate)

    assert "安全间隔期" in package.observations[0].text


def test_safety_block_explains_missing_redlines(tea_config: ProcessConfig) -> None:
    """红线未加载时的解释指向配置缺失，而不是含糊说"不安全"。"""
    gate = _gate(process_redlines_loaded=False, violations=[])
    package = _builder().build(HandoffReason.SAFETY_BLOCK, _state(), tea_config, gate_result=gate)

    assert "安全要求暂时没有完整加载" in package.observations[0].text


def test_safety_block_without_gate_result(tea_config: ProcessConfig) -> None:
    """闸门结论缺失时也给出一句可读解释，且不暴露任何具体方案。"""
    package = _builder().build(HandoffReason.SAFETY_BLOCK, _state(), tea_config, gate_result=None)

    assert package.observations[0].text
    assert "已记录" in package.observations[0].text


# ---- 第四组：字段口径 ----


def test_confirmed_facts_keys_are_display_labels(tea_config: ProcessConfig) -> None:
    """`confirmed_facts` 的键全部来自 `display_label`，不含任何槽位 key。"""
    state = _state(
        slots={
            "tea_symptom_part": _slot("tea_symptom_part", "叶片"),
            "tea_last_pick": _slot("tea_last_pick", "3 天前"),
        }
    )
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    labels = {spec.display_label for spec in tea_config.required_slots}
    keys = set(package.confirmed_facts)
    assert keys == {"茶树的哪个部位出问题了", "上次采摘是哪天"}
    assert keys <= labels
    assert not (keys & {spec.key for spec in tea_config.required_slots})


def test_confirmed_facts_skip_unfilled_slots(tea_config: ProcessConfig) -> None:
    """未填写与"问过答不出"都不进已确认信息，缺失由未收敛情形单独列出。"""
    state = _state(
        slots={
            "tea_symptom_part": _slot("tea_symptom_part", "叶片"),
            "tea_symptom_pattern": _slot("tea_symptom_pattern", None, status="unknown"),
            "tea_symptom_scope": _slot("tea_symptom_scope", None, status="missing"),
        }
    )
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert set(package.confirmed_facts) == {"茶树的哪个部位出问题了"}


def test_colloquialize_applies_map(tea_config: ProcessConfig) -> None:
    """术语口语化替换：展示与朗读文本里的"叶发病率"必须换成农户听得懂的说法。"""
    builder = _builder()
    converted = builder._colloquialize("叶发病率 40%，安全间隔期 7 天", tea_config)

    assert converted == "有多少叶子出了问题 40%，打完药要等多少天才能采 7 天"


def test_confirmed_facts_values_are_colloquialized(tea_config: ProcessConfig) -> None:
    """值也要过一遍 `colloquial_map`：上下文包最终是给农户读给农技人员听的。"""
    state = _state(slots={"tea_symptom_scope": _slot("tea_symptom_scope", "叶发病率约三成")})
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert package.confirmed_facts["大概有多少茶树这样"] == "有多少叶子出了问题约三成"


def test_problem_summary_uses_user_message(tea_config: ProcessConfig) -> None:
    """问题描述取农户本次输入的原话，截断到 200 字。"""
    state = _state(user_message="茶树叶子上有黑斑，" + "很担心" * 100)
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert package.problem_summary.startswith("茶树叶子上有黑斑，")
    assert len(package.problem_summary) == 200


def test_problem_summary_falls_back_to_messages(tea_config: ProcessConfig) -> None:
    """本次输入缺失时回落到会话首条消息，而不是空串。"""
    state = _state(user_message="", messages=[{"role": "user", "content": "叶子发黄"}])
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert package.problem_summary == "叶子发黄"


def test_excluded_read_from_diagnosis(tea_config: ProcessConfig) -> None:
    """已排除项直接读判定的 `excluded`，它是上下文包四项内容之一。"""
    state = _state(diagnosis={"excluded": ["生理性病害", "药害"]})
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert package.excluded == ["生理性病害", "药害"]


# ---- 第五组：朗读文本 ----


@pytest.mark.parametrize(
    "reason",
    [HandoffReason.OUT_OF_SCOPE, HandoffReason.SAFETY_BLOCK, HandoffReason.NOT_CONVERGED, HandoffReason.LEGAL_REPORT],
)
def test_all_reasons_produce_headline_and_voice(tea_config: ProcessConfig, reason: HandoffReason) -> None:
    """四种情形都产出非空的 headline 与 voice_text。"""
    state = _state(
        slots={"tea_symptom_part": _slot("tea_symptom_part", "叶片")},
        diagnosis={"out_of_scope": True, "boundary_reason": "超出范围", "excluded": ["缺素"]},
    )
    package = _builder().build(reason, state, tea_config, report_decision=_report_decision())

    assert package.headline.strip()
    assert package.voice_text.startswith(package.headline)
    assert len(package.voice_text) > len(package.headline)


def test_voice_text_limits_items(tea_config: ProcessConfig) -> None:
    """朗读文本会被整段读完，要点条数必须有上限。"""
    state = _state(slots={k: _slot(k, "有") for k in ("tea_symptom_part", "tea_symptom_pattern")})
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    # 缺失项 3 条（5 个必问 - 已填 2 个），未超上限；另加一句已确认信息
    assert package.voice_text.count("。") == 4

    many = _state(
        slots={},
        diagnosis={"out_of_scope": True, "boundary_reason": "边界原因"},
    )
    oversized = _builder().build(HandoffReason.OUT_OF_SCOPE, many, tea_config)
    # 超边界的观察要点固定 3 条 + 边界原因 1 条 = 4 条，仍在五条以内
    assert oversized.voice_text.count("。") == 4


def test_voice_text_truncates_facts_and_items(tea_config: ProcessConfig) -> None:
    """事实与要点各截断到五条，长度可控。"""
    builder = _builder()
    confirmed = {f"标签{i}": "值" for i in range(9)}
    items = [HandoffObservation(text=f"要点{i}") for i in range(9)]
    voice = builder._compose_voice("开头。", confirmed, items)

    assert voice.startswith("开头。")
    assert "标签4" in voice and "标签5" not in voice
    assert "要点4。" in voice and "要点5" not in voice


def test_voice_text_accepts_plain_strings(tea_config: ProcessConfig) -> None:
    """法定报告情形的要点是动作文本（纯字符串），与观察要点共用同一组装函数。"""
    voice = _builder()._compose_voice("需要马上报告。", {}, ["立即隔离发病猪只", "对栏舍消毒"])

    assert voice == "需要马上报告。立即隔离发病猪只。对栏舍消毒。"


def test_voice_text_includes_confirmed_facts(tea_config: ProcessConfig) -> None:
    """已确认信息以"已经了解到的情况是：..."起头，句短，适合朗读。"""
    state = _state(slots={"tea_symptom_part": _slot("tea_symptom_part", "叶片")})
    package = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert "已经了解到的情况是：茶树的哪个部位出问题了叶片。" in package.voice_text


# ---- 第六组：联系入口容错与摘要链接 ----


def test_contact_lookup_failure_leaves_empty(logs: _LogRecorder) -> None:
    """联系入口查询抛异常时留空并记 ERROR，绝不编造号码。"""
    builder = _builder(_FakeContacts(raise_exc=RuntimeError("目录服务不可用")))
    state = _state(gate_result=_gate().model_dump(mode="json"))
    package = builder.build(HandoffReason.SAFETY_BLOCK, state, get_process_config(ProcessId.TEA))

    assert package.contacts == []
    assert any(event == "handoff contacts lookup failed" for event, _ in logs.errors())


def test_summarizer_injected_into_package(tea_config: ProcessConfig) -> None:
    """摘要链接由注入的实现生成；未注入时留空而不是构造失败。"""
    seen: list[str] = []

    def summarizer(state: ConsultationState) -> str:
        seen.append(state["session_id"])
        return "https://example.test/summary/s1"

    state = _state()
    with_link = _builder(summarizer=summarizer).build(HandoffReason.NOT_CONVERGED, state, tea_config)
    without_link = _builder().build(HandoffReason.NOT_CONVERGED, state, tea_config)

    assert with_link.consult_summary_url == "https://example.test/summary/s1"
    assert seen == ["s1"]
    assert without_link.consult_summary_url is None


# ---- 第七组：原因判定与节点工厂 ----


def test_resolve_reason_prefers_legal_report(tea_config: ProcessConfig) -> None:
    """法定报告优先级最高：守卫命中即中止问诊，其余产出不得覆盖。"""
    state = _state(
        report_decision=_report_decision().model_dump(mode="json"),
        gate_result=_gate().model_dump(mode="json"),
        diagnosis={"out_of_scope": True},
    )

    assert resolve_handoff_reason(state) is HandoffReason.LEGAL_REPORT


def test_resolve_reason_order(tea_config: ProcessConfig) -> None:
    """优先级依次是：法定报告 > 安全拦截 > 超边界 > 未收敛；闸门放行时不算拦截。"""
    assert resolve_handoff_reason(_state(gate_result=_gate().model_dump(mode="json"))) is HandoffReason.SAFETY_BLOCK
    assert resolve_handoff_reason(_state(diagnosis={"out_of_scope": True})) is HandoffReason.OUT_OF_SCOPE
    assert resolve_handoff_reason(_state()) is HandoffReason.NOT_CONVERGED
    allowed = GateResult(
        passed=True, violations=[], checked_rules=[], process_redlines_loaded=True, decision="allow"
    ).model_dump(mode="json")
    assert resolve_handoff_reason(_state(gate_result=allowed)) is HandoffReason.NOT_CONVERGED


async def test_node_writes_declared_keys_only(tea_config: ProcessConfig) -> None:
    """节点只写声明的三个键，不改其他 state 字段。"""
    node = build_handoff_node(tea_config)
    update = await node(cast(ConsultationState, _state()))

    assert set(update) == {"handoff_package", "reply", "stage"}
    assert update["stage"] is ConsultStage.HANDOFF
    assert HandoffPackage.model_validate(update["handoff_package"]).reason is HandoffReason.NOT_CONVERGED
    assert update["reply"].startswith("问了这几轮还没能确定")


async def test_node_without_config_uses_state_process_id() -> None:
    """`config=None` 时节点按 state 上的 `process_id` 取配置（T2-25 的主图用法）。

    断言两点：用到了 `pig.yaml` 的槽位标签（证明配置取对了），且守卫已写入的
    `report_decision` 被节点读出并判为法定报告。
    """
    node = build_handoff_node()
    state = _state(
        process_id=ProcessId.PIG,
        user_message="昨天死了一头",
        report_decision=_report_decision().model_dump(mode="json"),
    )
    update = await node(cast(ConsultationState, state))

    package = HandoffPackage.model_validate(update["handoff_package"])
    assert package.reason is HandoffReason.LEGAL_REPORT
    assert "需要马上报告" in update["reply"]


async def test_node_uses_bound_config_slots() -> None:
    """传入 config 时按该流程的槽位标签生成缺失项（T2-26 的子图工厂用法）。"""
    node = build_handoff_node(get_process_config(ProcessId.PIG))
    state = _state(process_id=ProcessId.TEA, slots={})
    update = await node(cast(ConsultationState, state))
    voice = HandoffPackage.model_validate(update["handoff_package"]).voice_text

    assert "猪是什么样子" in voice
    assert "茶树的哪个部位出问题了" not in voice


async def test_node_falls_back_to_general_config() -> None:
    """未知流程标识回落到通用流程：转人工是兜底路径，配置读不到也要给出上下文包。"""
    node = build_handoff_node()
    update = await node(cast(ConsultationState, _state(process_id=cast(Any, "unknown-process"))))

    assert HandoffPackage.model_validate(update["handoff_package"]).headline


def test_build_node_signature_supports_both_forms() -> None:
    """两种调用形态都成立：T2-25 用无参、T2-26 的子图工厂传 config。"""
    params = list(inspect.signature(build_handoff_node).parameters)

    assert params[0] == "config"
    assert set(params[1:]) == {"contacts", "summarizer"}


async def test_node_is_replayable(tea_config: ProcessConfig) -> None:
    """同一状态连续 100 次构造，上下文包内容完全一致（纯规则实现，无模型参与）。"""
    node = build_handoff_node(tea_config)
    state = cast(
        ConsultationState,
        _state(slots={"tea_symptom_part": _slot("tea_symptom_part", "叶片")}, diagnosis={"excluded": ["缺素"]}),
    )

    first = await node(state)
    for _ in range(99):
        again = await node(state)
        assert again == first


def test_reply_contains_all_mandatory_actions_in_order(pig_config: ProcessConfig) -> None:
    """法定报告的强制动作一条都不能少，且顺序与契约一致（T2-28 的输出优先级要求）。"""
    actions = ["立即隔离发病猪只", "对栏舍消毒", "联系当地畜牧兽医部门报告"]
    state = _state(process_id=ProcessId.PIG)
    package = _builder().build(
        HandoffReason.LEGAL_REPORT, state, pig_config, report_decision=_report_decision(actions=actions)
    )
    reply = render_handoff_reply(package)

    positions = [reply.index(action) for action in actions]
    assert positions == sorted(positions)
    assert "1. 立即隔离发病猪只" in reply


def test_reply_renders_contact_without_phone() -> None:
    """联系入口没有电话时给出查询口径，而不是留白。"""
    package = HandoffPackage(
        reason=HandoffReason.LEGAL_REPORT,
        headline="需要马上报告",
        problem_summary="",
        contacts=[ContactInfo(kind="hotline", name="当地畜牧兽医部门", phone=None, note="请通过当地政府查询联系电话")],
        mandatory_actions=["立即隔离发病猪只"],
        voice_text="需要马上报告",
    )
    reply = render_handoff_reply(package)

    assert "请通过当地政府查询联系电话" in reply


def test_empty_directory_returns_nothing() -> None:
    """占位实现返回空列表而不是抛异常，让未接入联系入口时不至于打断整条链路。"""
    assert (
        _builder(cast(ContactDirectory, handoff_module._EmptyDirectory()))
        .build(HandoffReason.NOT_CONVERGED, _state(), get_process_config(ProcessId.TEA))
        .contacts
        == []
    )
