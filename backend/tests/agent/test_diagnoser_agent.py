"""T2-19：判定智能体的排除项校验、不确定来源补全与降级。

用例按七组组织：

1. 排除项校验：辣椒流程缺一个候选时抛 `ContractViolation` 并触发重试，重试仍缺则降级；
  结论本身就是候选时不要求它出现在排除项里；无易混淆项的流程不做该校验；
2. 不确定来源：证据集为空时强制 `uncertain`，面板未收敛时同样强制并记入来源，
   两者与模型自身判断的 `uncertain` 分开记录；
3. 术语档位：`terminology_profile` 恒等于 `process_config.process_id.value`，模型给的值被覆写；
4. 分数不出契约：`DiagnosisResult` 无分数字段，`internal_confidence` 的结果只落 state；
5. 回环判据：`judge_loops` 与 `round` 任一达到上限即不再回环，超边界一律不回环；
6. 超边界：`boundary_reason` 被补齐，面向农户的文案含 `basis` 中的内容；
7. 降级与 mock：模型不可用时降级为不确定且排除项等于配置要求的完整集合；mock 模式下
   `run` 返回满足契约的对象；全程零网络请求。

网关是替身（本智能体在网关面前是被调用方），流程配置取真实的辣椒与油菜流程。
"""

import json
from typing import Any, cast

import pytest
import respx

from app.agent.agents.base import AgentContext
from app.agent.agents.diagnoser import (
    _DEFAULT_OBSERVATION_POINT,
    _MAX_EVIDENCE_ROWS,
    DiagnoserAgent,
    build_diagnoser_prompt,
)
from app.agent.agents.exceptions import ContractViolation
from app.agent.budget import ConsultBudget
from app.agent.contracts import DiagnosisResult
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.llm import ModelGateway, build_gateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_MESSAGE = "我家辣椒突然蔫了，叶子还是绿的"
# 辣椒流程的易混淆项候选。结论取其中一个，另一个必须出现在排除项里。
_WILT = "枯萎病"
_BACTERIAL_WILT = "青枯病"


def _evidence(count: int = 2) -> list[dict]:
    """`state["evidence"]` 的形态：`EvidenceItem` 序列化后的字典。"""
    return [
        {
            "knowledge_id": f"kb_chili_{i:03d}",
            "title": f"辣椒土传病害判据 {i}",
            "content": "挤压切口渗出乳白色菌脓是青枯病的判据。" * 3,
            "process_id": "chili",
            "region_scope": "county:330102",
            "effective_date": "2025-03-01",
            "kind": "disease",
            "score": 0.9 - i * 0.01,
        }
        for i in range(1, count + 1)
    ]


def _payload(**overrides: Any) -> dict[str, Any]:
    """模型侧的最小合法输出。模型给的 `terminology_profile` 故意写成自由值，用于验证它被覆写。"""
    data: dict[str, Any] = {
        "conclusion": _BACTERIAL_WILT,
        "confidence_level": "preliminary",
        "basis": ["挤压切口渗出乳白色菌脓", "维管束黄褐色"],
        "uncertainty_source": [],
        "excluded": [_WILT],
        "terminology_profile": "model-chosen",
        "needs_more_info": [],
        "out_of_scope": False,
        "boundary_reason": None,
    }
    data.update(overrides)
    return data


def _result(**overrides: Any) -> DiagnosisResult:
    return DiagnosisResult.model_validate(_payload(**overrides))


def _ok(**overrides: Any) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象（适配器负责校验），不是裸字典。"""
    data = _payload(**overrides)
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=DiagnosisResult.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


def _parse_failure() -> ModelResponse:
    """解析失败：`parsed` 为空且带错误说明，基类据此触发重试并回填修复提示。"""
    return ModelResponse(
        content="（模型输出不是合法 JSON）",
        parsed=None,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1,
        error="parse_error",
    )


class _FakeGateway:
    """回放预设结果的网关替身。最后一次结果被后续所有调用复用。"""

    def __init__(self, results: list[ModelResponse | Exception]) -> None:
        self._results = results
        self.calls: list[tuple[object, list[dict], object]] = []

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.calls.append((task_type, list(messages), schema))
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def last_messages(self) -> list[dict]:
        return self.calls[-1][1]


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(scope="module")
def chili_config() -> ProcessConfig:
    return get_process_config(ProcessId.CHILI)


@pytest.fixture(scope="module")
def rapeseed_config() -> ProcessConfig:
    return get_process_config(ProcessId.RAPESEED)


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(ProcessId.TEA)


def _ctx(config: ProcessConfig) -> AgentContext:
    return AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.DIAGNOSE,
        trace_id="t-1",
        region_code="330102",
    )


@pytest.fixture()
def ctx(chili_config: ProcessConfig) -> AgentContext:
    return _ctx(chili_config)


def _agent(
    results: list[ModelResponse | Exception],
    *,
    budget: ConsultBudget | None = None,
) -> tuple[DiagnoserAgent, _FakeGateway]:
    gateway = _FakeGateway(results)
    return DiagnoserAgent(cast(ModelGateway, gateway), budget or ConsultBudget()), gateway


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _MESSAGE}],
    }
    state.update(overrides)
    return state


# ---- 第一组：排除项校验 ----


def test_conclusion_counts_as_coverage(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """结论本身是候选时不必出现在排除项里，另一个候选出现即通过。"""
    agent, _ = _agent([])

    agent.validate_output(_result(excluded=[_WILT]), _state(), ctx)


def test_empty_excluded_is_rejected(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """有易混淆项的流程必须输出 excluded：空列表即违约。"""
    agent, _ = _agent([])

    with pytest.raises(ContractViolation, match="必须输出 excluded"):
        agent.validate_output(_result(excluded=[]), _state(), ctx)


def test_partial_excluded_is_rejected(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """排除项不含任何候选时按"未显式排除"处理，且错误信息指出缺了谁。"""
    agent, _ = _agent([])

    with pytest.raises(ContractViolation, match="未显式排除"):
        agent.validate_output(_result(excluded=["不相关的东西"]), _state(), ctx)


def test_required_exclusions_follows_config(chili_config: ProcessConfig, tea_config: ProcessConfig) -> None:
    """必须排除的集合来自 `confusable_items` 中 `must_exclude` 的候选，随流程变化。"""
    agent, _ = _agent([])

    assert agent.required_exclusions(chili_config) == {_BACTERIAL_WILT, _WILT}
    assert agent.required_exclusions(tea_config) == {"侵染性病害", "生理性病害"}


def test_no_confusable_pair_skips_check(rapeseed_config: ProcessConfig) -> None:
    """没有易混淆项的流程不做排除项校验：空 `excluded` 是合法的。"""
    agent, _ = _agent([])

    agent.validate_output(_result(excluded=[]), _state(), _ctx(rapeseed_config))
    assert agent.required_exclusions(rapeseed_config) == set()


async def test_missing_exclusion_triggers_retry(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """缺一个候选时抛 `ContractViolation` 触发重试，第二次给对即可通过，不降级。"""
    agent, gateway = _agent([_ok(excluded=[]), _ok()])

    result = await agent.run(_state(evidence=_evidence()), ctx)

    assert len(gateway.calls) == 2
    assert result.excluded == [_WILT]
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "diagnoser", "mode": "degrade"}) == 0


async def test_exhausted_exclusion_retry_degrades(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """重试仍缺排除项则降级为不确定结论，降级产出同样过业务校验。"""
    agent, gateway = _agent([_ok(excluded=["不相关的东西"])])

    result = await agent.run(_state(evidence=_evidence()), ctx)

    # max_attempts=2 是"首次之外的额外次数"，故总计 3 次调用。
    assert len(gateway.calls) == 3
    assert result.confidence_level == "uncertain"
    assert set(result.excluded) == agent.required_exclusions(chili_config)
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "diagnoser", "mode": "degrade"}) == 1


async def test_parse_failure_triggers_retry_with_repair_hint(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """解析失败同样触发重试，且这次带上了修复提示（基类只对解析错误回填提示语）。

    业务校验失败（排除项缺失）也触发重试，但基类不回填提示语——错误发生在校验阶段而不是
    解析阶段，重试时提示词与首次相同。见 `test_missing_exclusion_triggers_retry`。
    """
    agent, gateway = _agent([_parse_failure(), _ok()])

    result = await agent.run(_state(evidence=_evidence()), ctx)

    assert len(gateway.calls) == 2
    messages = gateway.last_messages
    assert messages[-2]["role"] == "assistant"
    assert "parse_error" in messages[-1]["content"]
    assert result.excluded == [_WILT]


# ---- 第二组：不确定来源的补全 ----


async def test_empty_evidence_forces_uncertain(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """证据集为空时 `confidence_level` 被覆写为 `uncertain`，来源写明"未检索到支撑条目"。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(), ctx)

    assert result.confidence_level == "uncertain"
    assert "未检索到支撑条目" in result.uncertainty_source


async def test_evidence_present_keeps_model_confidence(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """有证据时不强行降级把握程度：覆写只在确有判据时发生。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(evidence=_evidence()), ctx)

    assert result.confidence_level == "preliminary"
    assert result.uncertainty_source == []


async def test_panel_force_uncertain_overrides(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """并行会诊未收敛时强制不确定，来源含"并行会诊"，与"没证据"分开记录。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(evidence=_evidence(), panel_force_uncertain=True), ctx)

    assert result.confidence_level == "uncertain"
    assert any("并行会诊" in source for source in result.uncertainty_source)


async def test_two_uncertain_sources_are_distinguished(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """无证据与面板未收敛是两条独立来源，同时命中时都要留下。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(panel_force_uncertain=True), ctx)

    assert "未检索到支撑条目" in result.uncertainty_source
    assert any("并行会诊" in source for source in result.uncertainty_source)


async def test_uncertain_without_reason_gets_fallback(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型自报不确定且既无来源也无待补信息时，补一条可照做的建议。

    用 `model_construct` 绕过契约校验构造这个中间态：正常路径下契约（T2-11）已拦住它，
    这里验证的是 `post_validate` 的重建在任何输入下都不会再次抛错。
    """
    agent, _ = _agent([])
    bare = DiagnosisResult.model_construct(
        conclusion="说不准",
        confidence_level="uncertain",
        basis=["症状不典型"],
        uncertainty_source=[],
        excluded=[_WILT],
        terminology_profile="model-chosen",
        needs_more_info=[],
        out_of_scope=False,
        boundary_reason=None,
    )

    result = await agent.post_validate(bare, _state(evidence=_evidence()), ctx)

    assert result.needs_more_info


# ---- 第三组：术语档位 ----


async def test_terminology_profile_follows_process(
    chili_config: ProcessConfig, rapeseed_config: ProcessConfig, ctx: AgentContext
) -> None:
    """`terminology_profile` 恒等于 `process_id.value`，模型给的值一律被覆写。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(evidence=_evidence()), ctx)
    other = await agent.run(_state(evidence=_evidence()), _ctx(rapeseed_config))

    assert result.terminology_profile == chili_config.process_id.value
    assert result.terminology_profile == "chili"
    assert other.terminology_profile == rapeseed_config.process_id.value


# ---- 第四组：分数不出契约 ----


def test_contract_has_no_numeric_confidence_field() -> None:
    """契约里不得有任何含 score / percent 字样的字段：把握程度只能是枚举。"""
    names = list(DiagnosisResult.model_fields)

    assert not [name for name in names if "score" in name.lower()]
    assert not [name for name in names if "percent" in name.lower()]
    assert "confidence_level" in names


def test_internal_confidence_stays_out_of_contract(ctx: AgentContext) -> None:
    """`internal_confidence` 的返回值不写入契约：`model_dump()` 里没有它，也没有浮点字段。"""
    agent, _ = _agent([])
    result = _result()

    score = agent.internal_confidence(result, 3)
    dumped = result.model_dump()

    assert score == pytest.approx(0.66)
    assert "confidence_score" not in dumped
    assert set(dumped) == set(DiagnosisResult.model_fields)
    assert not [value for value in dumped.values() if isinstance(value, float)]


def test_internal_confidence_is_capped(ctx: AgentContext) -> None:
    """分值随证据条数小幅上浮且有上限，档位是主导项。"""
    agent, _ = _agent([])
    uncertain = DiagnosisResult.model_construct(
        confidence_level="uncertain", basis=["症状不典型"], uncertainty_source=["未检索到支撑条目"]
    )

    assert agent.internal_confidence(uncertain, 0) == pytest.approx(0.3)
    assert agent.internal_confidence(_result(confidence_level="likely"), 0) == pytest.approx(0.85)
    assert agent.internal_confidence(_result(confidence_level="likely"), 99) > 0.85
    assert agent.internal_confidence(_result(confidence_level="likely"), 99) <= 1.0


async def test_score_is_written_to_state(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """分数落在 state 的 `confidence_score` 上供 T2-24 的条件边读取。"""
    agent, _ = _agent([_ok()])
    state = _state(evidence=_evidence(3))

    await agent.run(state, ctx)

    assert cast(dict[str, Any], state)["confidence_score"] == pytest.approx(0.66)


# ---- 第五组：回环判据 ----


def test_loop_back_when_confidence_low(ctx: AgentContext) -> None:
    """需要补信息且预算充足时回环补问一次。"""
    agent, _ = _agent([])

    assert agent.should_loop_back(_result(needs_more_info=["症状出现多久了"]), _state())


def test_no_loop_back_without_needs_more_info(ctx: AgentContext) -> None:
    """没有待补信息就没有回环的理由。"""
    agent, _ = _agent([])

    assert agent.should_loop_back(_result(), _state()) is False


def test_no_loop_back_when_judge_loops_exhausted(ctx: AgentContext) -> None:
    """`judge_loops` 达到上限时不再回环，即使仍待补信息。"""
    agent, _ = _agent([], budget=ConsultBudget(max_judge_loops=2))

    assert agent.should_loop_back(_result(needs_more_info=["还缺照片"]), _state(judge_loops=2)) is False
    assert agent.should_loop_back(_result(needs_more_info=["还缺照片"]), _state(judge_loops=1)) is True


def test_no_loop_back_when_rounds_exhausted(ctx: AgentContext) -> None:
    """问诊总轮次达到上限时不再回环：两个上限由不同机制维护，都要检查。"""
    agent, _ = _agent([], budget=ConsultBudget(max_interview_rounds=4))

    assert agent.should_loop_back(_result(needs_more_info=["还缺照片"]), _state(round=4)) is False
    assert agent.should_loop_back(_result(needs_more_info=["还缺照片"]), _state(round=3)) is True


def test_no_loop_back_when_out_of_scope(ctx: AgentContext) -> None:
    """超边界时不回环补问：问题超出能力范围，再问多少轮也答不了。"""
    agent, _ = _agent([])
    result = _result(out_of_scope=True, boundary_reason="涉及法定报告流程", needs_more_info=["还缺照片"])

    assert agent.should_loop_back(result, _state()) is False


# ---- 第六组：超边界 ----


async def test_boundary_reason_is_filled(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型判超边界但没给原因时由代码补齐：没有原因的边界判定在农户侧是一次无解释的拒答。"""
    agent, _ = _agent([])
    bare = DiagnosisResult.model_construct(
        conclusion="这是法定报告范围内的疫病",
        confidence_level="preliminary",
        basis=["疑似重大疫病"],
        uncertainty_source=[],
        excluded=[_WILT],
        terminology_profile="model-chosen",
        needs_more_info=[],
        out_of_scope=True,
        boundary_reason=None,
    )

    result = await agent.post_validate(bare, _state(evidence=_evidence()), ctx)

    assert result.out_of_scope is True
    assert result.boundary_reason


def test_boundary_notice_quotes_basis(chili_config: ProcessConfig) -> None:
    """面向农户的文案含 `basis` 中的内容：观察要点有出处，不是另生成一段泛泛建议。"""
    agent, _ = _agent([])
    result = _result(basis=["挤压切口渗出乳白色菌脓", "维管束黄褐色", "这段不该出现"])

    notice = agent.build_boundary_notice(result, chili_config)

    assert "挤压切口渗出乳白色菌脓" in notice
    assert "去当地农技站" in notice
    assert "这段不该出现" not in notice


def test_boundary_notice_falls_back_when_basis_empty(chili_config: ProcessConfig) -> None:
    """`basis` 意外为空时给一条可观察项，不让文案出现空位。"""
    agent, _ = _agent([])
    bare = DiagnosisResult.model_construct(basis=[], out_of_scope=True, boundary_reason="超出范围")

    notice = agent.build_boundary_notice(bare, chili_config)

    assert _DEFAULT_OBSERVATION_POINT in notice


# ---- 第七组：降级、提示词与 mock ----


async def test_degrade_fills_full_exclusion_set(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型不可用时降级：排除项等于配置要求的完整集合，结论为不确定且不推人工通道。"""
    agent, gateway = _agent([ValueError("网关未装配")])

    result = await agent.run(_state(), ctx)

    # ValueError 不在可重试集合里（只有契约违约与瞬时故障重试），故只调用一次。
    assert len(gateway.calls) == 1
    assert set(result.excluded) == agent.required_exclusions(chili_config)
    assert result.confidence_level == "uncertain"
    assert result.out_of_scope is False
    assert result.boundary_reason is None
    assert result.terminology_profile == "chili"
    assert result.needs_more_info
    assert "判定环节不可用" in result.uncertainty_source[0]


async def test_degrade_is_idempotent_under_validation(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """降级产出经 `post_validate` 重建后仍满足业务校验：排除项完整故不会二次降级。"""
    agent, _ = _agent([])

    result = await agent.degrade(_state(), ctx, "ValueError: 网关未装配")
    rebuilt = await agent.post_validate(result, _state(), ctx)

    assert rebuilt.excluded == result.excluded
    assert rebuilt.confidence_level == "uncertain"


async def test_no_confusable_degrade_leaves_excluded_empty(rapeseed_config: ProcessConfig) -> None:
    """无易混淆项的流程降级时排除项为空：不编造配置里没有的候选。"""
    agent, _ = _agent([])

    result = await agent.degrade(_state(), _ctx(rapeseed_config), "ValueError: 网关未装配")

    assert result.excluded == []
    assert result.terminology_profile == "rapeseed"


def test_prompt_forbids_percentages(chili_config: ProcessConfig) -> None:
    """提示词显式禁止百分比与分数：与契约层不含分数字段形成双保险。"""
    messages = build_diagnoser_prompt(chili_config, _state(evidence=_evidence()), _evidence())
    system = messages[0]["content"]

    assert [m["role"] for m in messages] == ["system", "user"]
    assert "不得输出百分比或分数" in system
    assert chili_config.role_definition in system
    for candidate in (_BACTERIAL_WILT, _WILT):
        assert candidate in system
    for metric_name in chili_config.terminology.display_metrics:
        assert metric_name in system
    assert chili_config.redlines[0].id in system


def test_prompt_injects_slots_facts_evidence_and_history(chili_config: ProcessConfig) -> None:
    """user 段注入已确认槽位、现场事实、证据摘要与档案用药史。"""
    state = _state(
        slots={},
        observations={"env_source": "none", "observed_facts": {"rain_3d": "连续降雨"}, "note": "暂无环境数据"},
        prescription_history=[{"date": "2025-06-01", "product": "某杀菌剂"}],
    )

    messages = build_diagnoser_prompt(chili_config, state, _evidence())
    user = messages[1]["content"]

    assert "连续降雨" in user
    assert "暂无环境数据" in user
    assert "辣椒土传病害判据 1" in user
    assert "某杀菌剂" in user


def test_prompt_marks_missing_evidence(chili_config: ProcessConfig) -> None:
    """证据为空时提示词里写明未命中，模型才会选择 uncertain 而不是硬给结论。"""
    user = build_diagnoser_prompt(chili_config, _state(), [])[1]["content"]

    assert "未命中" in user


def test_prompt_truncates_evidence_rows(chili_config: ProcessConfig) -> None:
    """证据条数有上限：长上下文会稀释模型对 Schema 的遵循度。"""
    user = build_diagnoser_prompt(chili_config, _state(), _evidence(20))[1]["content"]

    assert user.count("- 辣椒土传病害判据") == _MAX_EVIDENCE_ROWS
    assert "辣椒土传病害判据 7" not in user


def test_messages_use_state_evidence(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """`build_messages` 取自 state 里已核准的条目，不另行检索。"""
    agent, _ = _agent([])

    messages = agent.build_messages(_state(evidence=_evidence(1)), ctx)

    assert "辣椒土传病害判据 1" in messages[1]["content"]


async def test_run_under_mock_returns_contract_object(rapeseed_config: ProcessConfig) -> None:
    """`USE_MOCK_LLM=true` 下 `run` 返回满足契约的对象，术语档位仍由代码填入。"""
    agent = DiagnoserAgent(build_gateway(), ConsultBudget())

    result = await agent.run(_state(evidence=_evidence()), _ctx(rapeseed_config))

    assert isinstance(result, DiagnosisResult)
    assert result.confidence_level == "preliminary"
    assert result.terminology_profile == "rapeseed"


async def test_mock_with_confusables_falls_back_to_degrade(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """mock 的 `excluded` 为空且有易混淆项，重试仍不过即降级——不确定比未鉴别的确定结论安全。"""
    agent = DiagnoserAgent(build_gateway(), ConsultBudget())

    result = await agent.run(_state(evidence=_evidence()), ctx)

    assert result.confidence_level == "uncertain"
    assert set(result.excluded) == agent.required_exclusions(chili_config)


async def test_no_network_request_during_run(
    chili_config: ProcessConfig, ctx: AgentContext, respx_mock: respx.Router
) -> None:
    """判定全程不得发起任何 HTTP 请求：模型调用经注入的网关。"""
    agent, gateway = _agent([_ok()])

    await agent.run(_state(evidence=_evidence()), ctx)

    assert len(gateway.calls) == 1
    assert respx_mock.calls == []
