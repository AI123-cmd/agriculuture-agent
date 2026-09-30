"""T2-16：问诊智能体的候选域约束、进度提示与模板降级。

用例按五组组织：

1. 候选域约束：模型问了候选池之外的方向时抛 `ContractViolation` 并触发重试，重试仍越界
   则降级。这条约束是"跳过已知项"与"不漏必问项"的保障——两个要求由代码保证，模型只负责
   语言表达；
2. 进度提示与缺失标注：`stage_hint` 一律由代码覆写（模型爱写"再问两三个问题"），
   `confidence_note` 按环境数据与 `unknown` 槽位数判定；
3. 追问计数：本轮问过的槽位记进临时键，供筛选器剔除超限槽位；
4. 模板降级：模型不可用时用配置里的 `display_label` 直接提问，模板来源剔除已填、已答
   不清与追问超限的槽位；
5. 提示词：七段各自的内容都在，尤其是随流程变化的那四段。

网关是替身（本智能体在网关面前是被调用方），流程配置取真实的茶流程；另有一条零请求断言，
保证它不会绕过网关自己发请求。
"""

import json
from typing import Any, cast

import pytest
import respx

from app.agent.agents.base import AgentContext
from app.agent.agents.exceptions import AgentDegraded, ContractViolation
from app.agent.agents.interviewer import TEMPLATE_WHY, InterviewerAgent
from app.agent.budget import ConsultBudget
from app.agent.contracts import InterviewBatch
from app.agent.prompts.interviewer import TOP_K
from app.agent.state import ConsultationState, ProcessId, SlotValue, TaskType
from app.llm import ModelGateway, build_gateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_MESSAGE = "我家茶树叶子发黄，这几天一直没下雨"
_OUTSIDE = "tea_not_in_pool"
_MODEL_STAGE_HINT = "大概再问两三个问题就好了"


def _question(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "slot_key": "tea_symptom_part",
        "text": "茶树的哪个部位出问题了",
        "why": "看清症状才能判断是哪一类问题",
        "options": ["嫩芽", "叶片", "枝干", "根部"],
        "answer_type": "single",
    }
    data.update(overrides)
    return data


def _payload(**overrides: Any) -> dict[str, Any]:
    """模型侧的最小合法输出。`stage_hint` 故意写成模型的自由发挥，用于验证它被覆写。"""
    data: dict[str, Any] = {
        "questions": [_question()],
        "stage_hint": _MODEL_STAGE_HINT,
        "confidence_note": None,
    }
    data.update(overrides)
    return data


def _ok(**overrides: Any) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象（适配器负责校验），不是裸字典。"""
    data = _payload(**overrides)
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=InterviewBatch.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


def _outside(**overrides: Any) -> ModelResponse:
    """候选池之外的输出。池内容由 `run` 里的筛选器决定，故它不是配置里声明的槽位。"""
    return _ok(questions=[_question(slot_key=_OUTSIDE)], **overrides)


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
def tea_config() -> ProcessConfig:
    return get_process_config(ProcessId.TEA)


def _ctx(config: ProcessConfig) -> AgentContext:
    return AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.INTERVIEW,
        trace_id="t-1",
        region_code="330000",
    )


@pytest.fixture()
def ctx(tea_config: ProcessConfig) -> AgentContext:
    return _ctx(tea_config)


def _agent(
    results: list[ModelResponse | Exception],
    *,
    budget: ConsultBudget | None = None,
) -> tuple[InterviewerAgent, _FakeGateway]:
    gateway = _FakeGateway(results)
    return InterviewerAgent(cast(ModelGateway, gateway), budget or ConsultBudget()), gateway


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 0,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _MESSAGE}],
    }
    state.update(overrides)
    return state


def _slot(key: str, status: str, value: str | None = None, source: str = "user_answer") -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source=source)  # type: ignore[arg-type]


def _display_label(config: ProcessConfig, key: str) -> str:
    spec = config.slot_spec(key)
    assert spec is not None, f"配置里应声明槽位 {key}"
    return spec.display_label


# ---- 第一组：候选域约束 ----


async def test_out_of_pool_slot_key_triggers_retry(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型问了候选池之外的方向时触发重试，第二次给对即可通过。"""
    agent, gateway = _agent([_outside(), _ok()])

    result = await agent.run(_state(), ctx)

    assert len(gateway.calls) == 2
    assert [q.slot_key for q in result.questions] == ["tea_symptom_part"]
    # 重试没有落到降级：第二次的输出在候选池内，业务校验通过。
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "interviewer", "mode": "degrade"}) == 0


async def test_out_of_pool_exhausts_retries_then_degrades(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """重试仍越界则降级为模板问题，降级产出同样过业务校验。"""
    agent, gateway = _agent([_outside()])

    result = await agent.run(_state(), ctx)

    # max_attempts=2 是"首次之外的额外次数"，故总计 3 次调用。
    assert len(gateway.calls) == 3
    # 降级按 `ask_order` 取前两个必问槽位，文本直接用配置里的口语标签。
    keys = [q.slot_key for q in result.questions]
    assert keys == ["tea_symptom_part", "tea_symptom_pattern"]
    assert all(q.text == _display_label(tea_config, q.slot_key) for q in result.questions)
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "interviewer", "mode": "degrade"}) == 1


def test_validate_output_rejects_direction_outside_pool(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """`validate_output` 是候选域约束的执行点：越界即抛 `ContractViolation`。"""
    agent, _ = _agent([_ok()])
    state = _state(_candidate_pool_keys={"tea_symptom_part"})

    with pytest.raises(ContractViolation, match="候选池之外"):
        agent.validate_output(
            InterviewBatch.model_validate(_payload(questions=[_question(slot_key=_OUTSIDE)])), state, ctx
        )

    # 池内方向不抛异常。
    agent.validate_output(InterviewBatch.model_validate(_payload()), state, ctx)


async def test_run_writes_code_computed_pool_keys(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """调用模型之前先算好候选池并写入临时键，校验器因此不必重算。"""
    agent, _ = _agent([_ok()])
    state = _state()

    await agent.run(state, ctx)

    keys = cast(set[str], state.get("_candidate_pool_keys"))
    assert isinstance(keys, set)
    assert "tea_symptom_part" in keys
    assert _OUTSIDE not in keys
    assert len(keys) <= TOP_K
    assert keys <= {spec.key for spec in tea_config.required_slots}


# ---- 第二组：进度提示与缺失标注 ----


async def test_stage_hint_is_overwritten_by_code(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型写的进度提示被代码覆写为固定模板串。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(round=1), ctx)

    # 替身回放的正是模型自由发挥的那句，而返回的 `stage_hint` 已被代码覆写。
    assert result.stage_hint == "正在了解情况 2/4"
    assert _MODEL_STAGE_HINT != result.stage_hint


def test_compute_stage_hint_counts_and_clamps(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """轮次从 1 起算，且不超过最大问诊轮次（round=9 不出现越界文案）。"""
    agent, _ = _agent([_ok()])

    assert agent.compute_stage_hint(_state(round=0)) == "正在了解情况 1/4"
    assert agent.compute_stage_hint(_state(round=3)) == "正在了解情况 4/4"
    # 轮次已用尽后的补问仍显示 4/4，而不是 10/4。
    assert agent.compute_stage_hint(_state(round=9)) == "正在了解情况 4/4"


def test_stage_hint_follows_budget(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """文案里的总轮次来自预算，不是写死的 4。"""
    agent, _ = _agent([_ok()], budget=ConsultBudget(max_interview_rounds=2))

    assert agent.compute_stage_hint(_state(round=0)) == "正在了解情况 1/2"


async def test_confidence_note_when_no_env_data(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """完全没有环境数据时给出非空标注：农户据此知道判断主要靠自己的描述。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(observations={"env_source": "none"}), ctx)

    assert result.confidence_note


async def test_confidence_note_when_sensor_stale(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """传感器数据陈旧时的标注与"完全没有数据"分开。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(observations={"env_source": "sensor_stale", "note": "数据较旧"}), ctx)

    assert result.confidence_note
    assert "传感器" in result.confidence_note


async def test_confidence_note_is_none_with_sensor_and_no_unknown(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """有环境数据且没有"答不出"的槽位时不做额外标注。"""
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(observations={"env_source": "sensor"}), ctx)

    assert result.confidence_note is None


async def test_confidence_note_when_too_many_unknown_slots(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """三项以上答不出时主动说明把握程度：未知是合法答案，但结论的确定性要交代。"""
    slots = {key: _slot(key, "unknown") for key in ("tea_symptom_part", "tea_symptom_pattern", "tea_symptom_scope")}
    agent, _ = _agent([_ok()])

    result = await agent.run(_state(observations={"env_source": "sensor"}, slots=slots), ctx)

    assert result.confidence_note
    assert "把握" in result.confidence_note


async def test_model_confidence_note_is_kept_when_code_has_no_flag(
    tea_config: ProcessConfig, ctx: AgentContext
) -> None:
    """代码判定无需标注时不抹掉模型给的说明：覆写只在确有判据时发生。"""
    agent, _ = _agent([_ok(confidence_note="这几天连着下雨，先看看排水")])

    result = await agent.run(_state(observations={"env_source": "sensor"}), ctx)

    assert result.confidence_note == "这几天连着下雨，先看看排水"


# ---- 第三组：追问计数 ----


async def test_asked_slots_are_counted(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """本轮问过的槽位记进临时键，供筛选器剔除超限槽位。"""
    agent, _ = _agent([_ok(questions=[_question(), _question(slot_key="tea_symptom_pattern")])])
    state = _state()

    await agent.run(state, ctx)

    counters = cast(dict[str, int], state.get("_ask_counters"))
    assert counters == {"tea_symptom_part": 1, "tea_symptom_pattern": 1}


async def test_ask_counters_accumulate_across_rounds(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """同一槽位在多轮里被追问时计数累加，这是超限剔除的输入。"""
    agent, _ = _agent([_ok()])
    state = _state(_ask_counters={"tea_symptom_part": 2})

    await agent.run(state, ctx)

    assert cast(dict[str, int], state.get("_ask_counters"))["tea_symptom_part"] == 3


# ---- 第四组：模板降级 ----


async def test_degrade_returns_at_most_two_template_questions(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型不可用时降级：不超过两个问题，文本取配置的 `display_label`。"""
    agent, gateway = _agent([ValueError("网关未装配")])

    result = await agent.run(_state(), ctx)

    # ValueError 不在可重试集合里（只有契约违约与瞬时故障重试），故只调用一次。
    assert len(gateway.calls) == 1
    assert 1 <= len(result.questions) <= 2
    for question in result.questions:
        assert question.text == _display_label(tea_config, question.slot_key)
        assert question.why
    assert result.confidence_note
    assert result.stage_hint == "正在了解情况 1/4"


def test_degrade_questions_carry_options_and_answer_type(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """有选项的槽位降级后仍是单选，没选项的槽位只能让农户自己说。"""
    agent, _ = _agent([_ok()])

    pool = agent._template_pool(_state(), tea_config)  # noqa: SLF001

    assert [spec.key for spec in pool[:2]] == ["tea_symptom_part", "tea_symptom_pattern"]
    assert pool[0].options


def test_template_pool_excludes_filled_and_unknown(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """已确认与"农户答不出"的槽位都不再出现在模板问题里。"""
    agent, _ = _agent([_ok()])
    slots = {
        "tea_symptom_part": _slot("tea_symptom_part", "filled", "叶片"),
        "tea_symptom_pattern": _slot("tea_symptom_pattern", "unknown"),
    }

    pool = agent._template_pool(_state(slots=slots), tea_config)  # noqa: SLF001

    keys = [spec.key for spec in pool]
    assert keys[0] == "tea_symptom_scope"
    assert "tea_symptom_part" not in keys
    assert "tea_symptom_pattern" not in keys


def test_template_pool_excludes_slots_over_ask_limit(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """追问次数超过上限的槽位不再出现在模板问题里（超限即应转 unknown，不再问）。"""
    agent, _ = _agent([_ok()])
    budget = ConsultBudget()
    state = _state(_ask_counters={"tea_symptom_part": budget.max_slot_rounds + 1})

    pool = agent._template_pool(state, tea_config)  # noqa: SLF001

    assert "tea_symptom_part" not in [spec.key for spec in pool]


def test_template_pool_keeps_slot_at_limit(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """恰好等于上限仍可问：超限才剔除，边界不提前一格（与筛选器口径一致）。"""
    agent, _ = _agent([_ok()])
    budget = ConsultBudget()
    state = _state(_ask_counters={"tea_symptom_part": budget.max_slot_rounds})

    pool = agent._template_pool(state, tea_config)  # noqa: SLF001

    assert "tea_symptom_part" in [spec.key for spec in pool]


def test_template_pool_only_contains_required_slots(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """模板问题只取必问槽位：非必问项在模型不可用时问出来只会增加农户负担。"""
    agent, _ = _agent([_ok()])

    keys = [spec.key for spec in agent._template_pool(_state(), tea_config)]  # noqa: SLF001

    assert "tea_fertilize" not in keys
    assert "tea_shade_level" not in keys
    assert keys == [spec.key for spec in sorted(tea_config.required_slots, key=lambda s: s.ask_order) if spec.required]


async def test_degrade_raises_when_no_template_question_available(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """必问项都已答过时没有模板问题可问，抛 `AgentDegraded` 交由主图推进。"""
    agent, _ = _agent([ValueError("网关未装配")])
    slots = {spec.key: _slot(spec.key, "filled", "已确认") for spec in tea_config.required_slots if spec.required}

    with pytest.raises(AgentDegraded):
        await agent.run(_state(slots=slots), ctx)


async def test_template_why_is_always_non_empty(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """每个槽位分类都有可读的"为什么问"：表里有就取表，没有则回落到通用句。

    水稻与养猪流程的必问项含"苗情"，而 `TEMPLATE_WHY` 只登记了五个分类，故回落到
    `_DEFAULT_WHY` 是必须保留的路径——降级问题缺了 `why` 会直接撞上契约的必填校验。
    """
    assert {spec.group.value for spec in tea_config.required_slots if spec.required} <= set(TEMPLATE_WHY)

    for process_id in (ProcessId.RICE, ProcessId.PIG, ProcessId.WEED):
        config = get_process_config(process_id)
        agent, _ = _agent([_ok()])
        batch = await agent.degrade(_state(), _ctx(config), "模型不可用")
        assert batch.questions
        assert all(q.why.strip() for q in batch.questions)
        assert all(q.text == _display_label(config, q.slot_key) for q in batch.questions)


# ---- 第五组：提示词 ----


async def test_prompt_carries_config_driven_sections(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """系统提示含角色定位、指标口径、口语映射与至少一个易混淆项候选。"""
    agent, gateway = _agent([_ok()])

    await agent.run(_state(), ctx)

    system = gateway.last_messages[0]["content"]
    assert tea_config.role_definition in system
    for metric in tea_config.terminology.display_metrics:
        assert metric in system
    for spoken in tea_config.terminology.colloquial_map.values():
        assert spoken in system
    candidates = [name for pair in tea_config.confusable_items for name in pair.candidates]
    assert any(name in system for name in candidates)
    # 硬规则：上限两问、不得问已知、不得换措辞重问。
    assert "每轮最多问 2 个问题" in system
    assert "不得询问已经知道答案的信息" in system
    assert "换个说法再问一遍" in system


async def test_prompt_lists_candidates_with_slot_keys(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """候选方向带槽位键与选项进入提示词：模型才知道答案往哪个槽位写。"""
    agent, gateway = _agent([_ok()])

    await agent.run(_state(), ctx)

    user = gateway.last_messages[-1]["content"]
    assert "槽位 tea_symptom_part" in user
    assert "选项：嫩芽、叶片、枝干、根部" in user
    assert "330000" in user


async def test_prompt_includes_existing_slots_and_observations(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """已确认信息与环境事实按口语标签注入，不用槽位键。"""
    agent, gateway = _agent([_ok()])
    state = _state(
        slots={"tea_symptom_part": _slot("tea_symptom_part", "filled", "叶片")},
        observations={"env_source": "none", "observed_facts": {"rain_last_3d": "近三天没有下雨"}},
    )

    await agent.run(state, ctx)

    user = gateway.last_messages[-1]["content"]
    assert f"{_display_label(tea_config, 'tea_symptom_part')}=叶片" in user
    assert "rain_last_3d=近三天没有下雨" in user


# ---- 边界：增强追问、mock 模式与零网络请求 ----


def test_include_offline_questions_only_when_no_env_data(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """只有完全没有环境数据时才把 `offline_questions` 的目标槽位纳入候选。"""
    agent, _ = _agent([_ok()])

    without_env = agent.include_offline_questions(_state(observations={"env_source": "none"}), tea_config)
    stale = agent.include_offline_questions(_state(observations={"env_source": "sensor_stale"}), tea_config)
    with_sensor = agent.include_offline_questions(_state(observations={"env_source": "sensor"}), tea_config)

    assert [spec.key for spec in without_env] == ["tea_shade_level"]
    assert stale == []
    assert with_sensor == []


async def test_run_under_mock_returns_contract_object(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """`USE_MOCK_LLM=true` 下 `run` 返回满足契约的对象，进度提示仍由代码给出。"""
    agent = InterviewerAgent(build_gateway(), ConsultBudget())

    result = await agent.run(_state(), ctx)

    assert isinstance(result, InterviewBatch)
    assert result.questions
    assert result.stage_hint == "正在了解情况 1/4"


async def test_no_network_request_during_run(
    tea_config: ProcessConfig, ctx: AgentContext, respx_mock: respx.Router
) -> None:
    """问诊全程不得发起任何 HTTP 请求：模型调用一律经注入的网关。"""
    agent, gateway = _agent([_ok()])

    await agent.run(_state(), ctx)

    assert len(gateway.calls) == 1
    assert respx_mock.calls == []
