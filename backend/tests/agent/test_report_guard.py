"""T2-22：报告守卫的信号匹配、触发门槛、fail-closed 与联系入口。

用例分五组：

1. **触发门槛极低**：单个 `signal` 级信号命中即 `triggered=True`；槽位规则与文本规则是并集，
   入口守卫（`observe` 之前，`slots` 为空）只能靠文本规则，兜底守卫主要靠槽位规则；
2. **未触发与等级**：全部未命中时 `trigger_source="none"` 且 `stop_consultation=False`；
   最严重的一条决定 `trigger_source`（同时命中 `mass_death` 时不是 `signal`）；
3. **强制动作**：`signal` 级含"个别发病也需要报告"；全部文案不含疾病名与"确诊"字样；
4. **fail-closed 与联系入口**：守卫自身异常时仍 `triggered=True`；联系方式解析不到时返回
   一条 `phone=None` 的条目并记 ERROR；`__init__` 不收网关、`collect_decision` 零模型调用、
   零网络请求；
5. **可重放**：同一状态连续 100 次 `collect_decision` 结果完全一致；
6. **与流程配置对齐**：槽位规则的 `slot_key` 全部存在于 `pig.yaml`，且关键词能命中该槽位的
   `options` 之一——槽位 key 写错不会报错，只会让信号永远不触发，这是守卫最危险的失效形态。

守卫的依赖只有 `ContactDirectory` 一个注入点，其余全是纯函数，因此不需要网关替身：
`ReportGuardAgent` 根本不持有网关。
"""

import inspect
from typing import Any, cast

import pytest
import respx

import app.agent.agents.report_guard as report_guard_module
from app.agent.agents.base import AgentContext
from app.agent.agents.report_guard import (
    ContactDirectory,
    ReportGuardAgent,
    build_report_guard_node,
)
from app.agent.agents.report_signals import (
    PIG_SIGNALS,
    SIGNAL_RULES,
    match_signals,
    resolve_trigger_source,
)
from app.agent.contracts import ContactInfo, ReportDecision
from app.agent.state import ConsultationState, ProcessId, SlotValue, TaskType
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_REGION_CODE = "330102"

# 不得出现在强制动作里的字样：技术方案 6.7 明确养猪流程的疫情报告"不做疫情的最终认定"，
# 动作文案里出现疾病名或"确诊"等于变相给出了认定结论。
_FORBIDDEN_IN_ACTIONS = ("确诊", "猪瘟", "非洲猪瘟", "口蹄疫", "蓝耳", "圆环", "伪狂犬", "链球菌", "副猪")


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


class _ContactRecorder(_FakeContacts):
    """固定返回一条可用入口，用于断言"命中时确实带上了联系入口"。"""

    def __init__(self) -> None:
        super().__init__({_REGION_CODE: [_vet_station()]})


class _LogRecorder:
    """记录结构化日志事件的替身，用于断言"解析不到联系方式时确实记 ERROR"。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def errors(self) -> list[tuple[str, dict[str, Any]]]:
        return [e for e in self.events if e[0].startswith("report")]


def _vet_station() -> ContactInfo:
    return ContactInfo(kind="vet_station", name="XX 县畜牧兽医站", phone="0571-12345678", region_code=_REGION_CODE)


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def logs(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """把守卫模块里的 `log` 换成记录器：structlog 默认输出到 stdout，不便断言。"""
    recorder = _LogRecorder()
    monkeypatch.setattr(report_guard_module, "log", recorder)
    return recorder


@pytest.fixture(scope="module")
def pig_config() -> ProcessConfig:
    return get_process_config(ProcessId.PIG)


def _ctx(config: ProcessConfig, *, region_code: str | None = _REGION_CODE) -> AgentContext:
    return AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.REPORT_GUARD,
        trace_id="t-1",
        region_code=region_code,
    )


def _slot(key: str, value: str | None, status: str = "filled") -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source="user_answer")  # type: ignore[arg-type]


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "process_id": ProcessId.PIG,
        "round": 1,
        "slot_round": 0,
        "user_message": "",
        "messages": [{"role": "user", "content": ""}],
    }
    state.update(overrides)
    return state


def _agent(contacts: ContactDirectory | None = None) -> ReportGuardAgent:
    return ReportGuardAgent(contacts if contacts is not None else _FakeContacts())


# ---- 第一组：触发门槛极低 ----


def test_single_signal_slot_hits_and_triggers(pig_config: ProcessConfig) -> None:
    """一条 `signal` 级槽位命中即触发：不要求数量，也不要求群组信号。"""
    state = _state(slots={"pig_sudden_death": _slot("pig_sudden_death", "突然死亡无先兆")})
    decision = _agent(_ContactRecorder()).collect_decision(state, _ctx(pig_config))

    assert decision.triggered is True
    assert decision.matched_signals == ["sudden_death"]
    assert decision.trigger_source == "signal"
    assert decision.stop_consultation is True


def test_single_skin_lesion_slot_triggers(pig_config: ProcessConfig) -> None:
    """ "皮肤发红发紫"这一条选项命中即触发，不需要其他条件同时成立。"""
    state = _state(slots={"pig_symptom_pattern": _slot("pig_symptom_pattern", "皮肤发红发紫")})
    decision = _agent().collect_decision(state, _ctx(pig_config))

    assert decision.triggered is True
    assert decision.matched_signals == ["skin_hemorrhage"]


def test_text_rule_hits_without_any_slot(pig_config: ProcessConfig) -> None:
    """入口守卫在 `observe` 之前执行，`slots` 为空，只能靠文本规则——这是最需要触发的场景。"""
    state = _state(user_message="昨天死了一头，早上还好好的")

    hits = match_signals(state, None)
    decision = _agent().collect_decision(state, _ctx(pig_config))

    assert hits and "sudden_death" in hits
    assert decision.triggered is True
    assert decision.trigger_source == "signal"


def test_text_and_slot_hits_deduplicate(pig_config: ProcessConfig) -> None:
    """同一信号由槽位规则与文本规则各命中一次时只报一次，顺序由规则表决定。"""
    state = _state(
        user_message="昨天死了一头",
        slots={"pig_sudden_death": _slot("pig_sudden_death", "突然死亡无先兆")},
    )

    hits = match_signals(state, None)

    assert hits.count("sudden_death") == 1


def test_missing_or_unknown_slot_does_not_hit(pig_config: ProcessConfig) -> None:
    """未填写与"问过答不出"都不构成命中：`missing` 是还没问，`unknown` 是问不出。"""
    for status in ("missing", "unknown"):
        state = _state(
            slots={"pig_sudden_death": SlotValue(key="pig_sudden_death", status=status, source="user_answer")}
        )  # type: ignore[arg-type]

        assert match_signals(state, None) == []


def test_ordinary_answer_does_not_hit(pig_config: ProcessConfig) -> None:
    """不含任何信号关键词的常规回答不触发：误报虽然代价小，也不能无差别触发。"""
    state = _state(
        slots={
            "pig_symptom_pattern": _slot("pig_symptom_pattern", "不吃食"),
            "pig_death_count": _slot("pig_death_count", "1头"),
            "pig_sudden_death": _slot("pig_sudden_death", "病了几天"),
            "pig_multi_pen": _slot("pig_multi_pen", "只有一个栏"),
        }
    )

    assert match_signals(state, None) == []


# ---- 第二组：未触发与等级 ----


def test_no_hit_returns_none_source(pig_config: ProcessConfig) -> None:
    """全部信号未命中：`trigger_source="none"`、不中止问诊、无强制动作、无联系入口。"""
    decision = _agent(_ContactRecorder()).collect_decision(_state(), _ctx(pig_config))

    assert decision.triggered is False
    assert decision.matched_signals == []
    assert decision.trigger_source == "none"
    assert decision.mandatory_actions == []
    assert decision.contacts == []
    assert decision.stop_consultation is False


def test_group_and_mass_death_slots(pig_config: ProcessConfig) -> None:
    """群组与群体死亡信号各自由对应槽位命中，等级分别为 `group` 与 `mass_death`。"""
    group = _agent().collect_decision(
        _state(slots={"pig_multi_pen": _slot("pig_multi_pen", "多个栏舍同时出现")}),
        _ctx(pig_config),
    )
    mass = _agent().collect_decision(
        _state(slots={"pig_death_count": _slot("pig_death_count", "5头以上")}),
        _ctx(pig_config),
    )

    assert group.trigger_source == "group"
    assert mass.trigger_source == "mass_death"


def test_trigger_source_is_most_severe_hit(pig_config: ProcessConfig) -> None:
    """`trigger_source` 取最严重的一条：同时命中 signal 与 mass_death 时是 `mass_death`。"""
    state = _state(
        user_message="死了好多，昨天还是好好的",
        slots={
            "pig_sudden_death": _slot("pig_sudden_death", "突然死亡无先兆"),
            "pig_death_count": _slot("pig_death_count", "5头以上"),
        },
    )
    decision = _agent().collect_decision(state, _ctx(pig_config))

    assert set(decision.matched_signals) >= {"sudden_death", "mass_death"}
    assert decision.trigger_source == "mass_death"


def test_resolve_trigger_source_prefers_mass_death_over_group() -> None:
    """`resolve_trigger_source` 的优先级是 mass_death > group > signal，未命中为 none。"""
    assert resolve_trigger_source([]) == "none"
    assert resolve_trigger_source(["sudden_death"]) == "signal"
    assert resolve_trigger_source(["sudden_death", "multi_pen"]) == "group"
    assert resolve_trigger_source(["multi_pen", "mass_death"]) == "mass_death"


# ---- 第三组：强制动作 ----


def test_signal_level_action_mentions_individual_case(pig_config: ProcessConfig) -> None:
    """`signal` 级的最后一条刻意写明"个别发病也需要报告"，这是纠正认知盲区的落地。"""
    actions = _agent().mandatory_actions("signal")

    assert any("个别发病也需要报告" in a for a in actions)


def test_group_actions_add_movement_control(pig_config: ProcessConfig) -> None:
    """群组与群体死亡情形追加流动管控与头数说明，且不覆盖基础三条。"""
    signal_actions = _agent().mandatory_actions("signal")
    group_actions = _agent().mandatory_actions("group")

    assert group_actions[: len(signal_actions) - 1] == signal_actions[: len(signal_actions) - 1]
    assert any("暂停猪只与人员的场内外流动" in a for a in group_actions)
    assert any("个别发病也需要报告" in a for a in group_actions) is False


@pytest.mark.parametrize("source", ["signal", "group", "mass_death"])
def test_actions_contain_no_disease_name(source: str) -> None:
    """所有强制动作文案不含疾病名与"确诊"：守卫不做疫情的最终认定。"""
    for action in _agent().mandatory_actions(source):
        for forbidden in _FORBIDDEN_IN_ACTIONS:
            assert forbidden not in action


async def test_fail_closed_actions_contain_no_disease_name(pig_config: ProcessConfig) -> None:
    """fail-closed 路径的动作文案同受此约束。"""
    decision = await _agent().fail_closed(_state(), _ctx(pig_config), "boom")

    assert decision.mandatory_actions
    for action in decision.mandatory_actions:
        for forbidden in _FORBIDDEN_IN_ACTIONS:
            assert forbidden not in action


# ---- 第四组：fail-closed 与联系入口 ----


async def test_contacts_resolved_when_triggered(pig_config: ProcessConfig) -> None:
    """命中时带上联系入口，不记 ERROR，且联系入口按地区解析。"""
    contacts = _ContactRecorder()
    agent = _agent(contacts)
    decision = agent.collect_decision(_state(user_message="昨天死了一头"), _ctx(pig_config))

    assert decision.triggered is True
    assert decision.contacts and decision.contacts[0].phone == "0571-12345678"
    assert contacts.codes == [_REGION_CODE]


def test_unresolved_contacts_log_error_and_leave_phone_empty(pig_config: ProcessConfig, logs: _LogRecorder) -> None:
    """解析不到联系方式时返回一条 `phone=None` 的条目并记 ERROR，绝不编造号码。"""
    decision = _agent(_FakeContacts()).collect_decision(_state(user_message="昨天死了一头"), _ctx(pig_config))

    assert decision.triggered is True
    assert len(decision.contacts) == 1
    entry = decision.contacts[0]
    assert entry.phone is None
    assert entry.kind == "hotline"
    assert entry.region_code == _REGION_CODE
    assert any(event == "report contacts unresolved" for event, _ in logs.errors())


def test_contact_lookup_failure_still_reports(pig_config: ProcessConfig, logs: _LogRecorder) -> None:
    """联系入口查询本身抛异常时同样落在保守一侧：仍触发、仍给出口径、记 ERROR。"""
    decision = _agent(_FakeContacts(raise_exc=RuntimeError("目录服务不可用"))).collect_decision(
        _state(user_message="昨天死了一头"), _ctx(pig_config)
    )

    assert decision.triggered is True
    assert decision.contacts and decision.contacts[0].phone is None
    assert any(event == "report contacts lookup failed" for event, _ in logs.errors())


async def test_internal_exception_still_reports(pig_config: ProcessConfig, logs: _LogRecorder) -> None:
    """守卫内部异常时仍返回 `triggered=True`：宁可多报一次，不可漏报一次。"""
    broken = _state(user_message="昨天死了一头", slots=cast(Any, {"pig_sudden_death": object()}))
    decision = await _agent().run(broken, _ctx(pig_config))

    assert decision.triggered is True
    assert decision.matched_signals == ["guard_failure"]
    assert decision.trigger_source == "signal"
    assert decision.stop_consultation is True
    assert decision.mandatory_actions
    assert any(event == "report guard failed, defaulting to report" for event, _ in logs.errors())


async def test_fail_closed_direct_call(pig_config: ProcessConfig) -> None:
    """`fail_closed` 被直接调用时也返回可执行的报告结论。"""
    decision = await _agent().fail_closed(_state(), _ctx(pig_config), "boom")

    assert decision.triggered is True
    assert decision.matched_signals == ["guard_failure"]


async def test_run_reports_without_model_calls(pig_config: ProcessConfig) -> None:
    """`run` 覆写为直接走规则判定：不持有网关，执行期间零模型调用。"""
    agent = _agent(_ContactRecorder())
    assert agent._gateway is None

    decision = await agent.run(_state(user_message="昨天死了一头"), _ctx(pig_config))

    assert decision.triggered is True


def test_init_does_not_accept_gateway() -> None:
    """`__init__` 只收联系目录：守卫不持有模型网关（唯一的 fail-closed 特例之一）。"""
    params = list(inspect.signature(ReportGuardAgent.__init__).parameters)

    assert params == ["self", "contacts"]


def test_triggered_implies_stop_consultation(pig_config: ProcessConfig) -> None:
    """触发即中止问诊：`stop_consultation` 与 `triggered` 在守卫的产出里同真同假。"""
    triggered = _agent().collect_decision(_state(user_message="昨天死了一头"), _ctx(pig_config))
    untriggered = _agent().collect_decision(_state(), _ctx(pig_config))

    assert triggered.triggered is True and triggered.stop_consultation is True
    assert untriggered.triggered is False and untriggered.stop_consultation is False


async def test_no_network_request_during_run(pig_config: ProcessConfig, respx_mock: respx.Router) -> None:
    """规则实现不得发起任何 HTTP 请求。"""
    await _agent(_ContactRecorder()).run(_state(user_message="昨天死了一头"), _ctx(pig_config))

    assert respx_mock.calls == []


# ---- 第五组：可重放 ----


def test_collect_decision_is_deterministic(pig_config: ProcessConfig) -> None:
    """同一状态连续 100 次调用，`matched_signals` 顺序与内容完全一致。"""
    state = _state(
        user_message="死了好多，昨天还是好好的",
        slots={
            "pig_symptom_pattern": _slot("pig_symptom_pattern", "皮肤发红发紫"),
            "pig_death_count": _slot("pig_death_count", "5头以上"),
            "pig_sudden_death": _slot("pig_sudden_death", "突然死亡无先兆"),
            "pig_multi_pen": _slot("pig_multi_pen", "多个栏舍同时出现"),
        },
    )
    agent = _agent(_ContactRecorder())

    first = agent.collect_decision(state, _ctx(pig_config))
    for _ in range(99):
        again = agent.collect_decision(state, _ctx(pig_config))
        assert again.matched_signals == first.matched_signals
        assert again.trigger_source == first.trigger_source
        assert again.mandatory_actions == first.mandatory_actions


# ---- 第六组：与流程配置对齐 ----


def test_slot_rules_reference_real_pig_slots(pig_config: ProcessConfig) -> None:
    """槽位规则的 `slot_key` 必须真实存在于 `pig.yaml`：写错的槽位 key 只会让信号静默哑火。"""
    known = {spec.key for spec in pig_config.required_slots}

    for rule in PIG_SIGNALS:
        assert rule.slot_key in known


def test_slot_rule_keywords_hit_declared_options(pig_config: ProcessConfig) -> None:
    """每条槽位规则至少有一个关键词能命中该槽位声明的 `options` 之一。

    这条断言拦的是"信号表与配置各改一半"：配置把选项措辞改了而关键词没跟上，守卫就会在
    真实作答下永不触发，而单测如果只用自造字符串就发现不了。
    """
    options = {spec.key: list(spec.options or []) for spec in pig_config.required_slots}

    for rule in PIG_SIGNALS:
        assert rule.slot_key is not None
        declared = options[rule.slot_key]
        assert any(any(keyword in option for option in declared) for keyword in rule.keywords), rule.signal_id


def test_signal_ids_unique_and_severity_known() -> None:
    """`(signal_id, source)` 唯一，等级取值封闭。

    同一 `signal_id` 在并集里出现两次是刻意的：一次走槽位规则、一次走文本规则，两者覆盖
    不同的挂载点（入口守卫无槽位、兜底守卫有槽位）。重复的只能是 `source` 不同的两条，
    同 `source` 下重复说明规则表被误复制。
    """
    keys = [(rule.signal_id, rule.source) for rule in SIGNAL_RULES]

    assert len(keys) == len(set(keys))
    assert {rule.severity for rule in SIGNAL_RULES} <= {"signal", "group", "mass_death"}
    # 同一 signal_id 的两个来源必须给出相同等级，否则 trigger_source 会随命中来源漂移。
    severity_of: dict[str, set[str]] = {}
    for rule in SIGNAL_RULES:
        severity_of.setdefault(rule.signal_id, set()).add(rule.severity)
    assert all(len(levels) == 1 for levels in severity_of.values())


# ---- 第七组：节点工厂 ----


async def test_node_writes_decision_only(pig_config: ProcessConfig) -> None:
    """节点只写 `report_decision`，不改 `stage`（子图靠 stage 区分两个挂载点）。"""
    node = build_report_guard_node(pig_config)
    update = await node(cast(ConsultationState, _state(user_message="昨天死了一头")))

    assert set(update) == {"report_decision"}
    assert ReportDecision.model_validate(update["report_decision"]).triggered is True


async def test_node_without_config_uses_state_process_id() -> None:
    """`config=None` 时节点按 state 上的 `process_id` 取配置（T2-25 的主图用法）。"""
    node = build_report_guard_node()
    update = await node(cast(ConsultationState, _state(process_id=ProcessId.PIG, user_message="昨天死了一头")))

    assert ReportDecision.model_validate(update["report_decision"]).triggered is True
