"""T2-14：分诊智能体的分流、对象确认与降级。

用例分三组，对应验收标准里的三类主用例，外加五条契约级断言：

1. 分流：输入含作物词时分到对应流程；
2. 对象确认：档案流程与农户表述不符时 `object_confirmed` 被代码覆写为 False；
3. 降级：网关失败时回退 `general` 且 `fallback_reason` 非空。

另有一组断言直接盯住这条硬约束的实现方式：`TriageResult` 里不存在承载问题的字段、系统
提示包含七条流程、`slot_gaps` 由代码计算且排除可自动填充的槽位。

网关用替身回放预设结果：网关在本层是**被调用方**，它内部走 HTTP 还是走 mock 与分诊行为
无关；同时加一条 `respx` 零请求断言，保证分诊不会绕过网关自己发请求。
"""

import json
from typing import Any, cast

import pytest
import respx

from app.agent.agents.base import AgentContext
from app.agent.agents.triage import TriageAgent, run_triage
from app.agent.contracts import TriageResult
from app.agent.state import ConsultationState, ProcessId, SlotValue, TaskType
from app.llm import ModelGateway, build_gateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.models import ProcessConfig

_TEA_MESSAGE = "我家茶树叶子发黄"


def _payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "process_id": "tea",
        "object_confirmed": True,
        "object_label": "茶树·2亩",
        "slot_gaps": [],
        "profile_ready": True,
    }
    data.update(overrides)
    return data


def _ok(**overrides: Any) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象（适配器负责校验），不是裸字典。"""
    data = _payload(**overrides)
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=TriageResult.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
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
def configs() -> dict[ProcessId, ProcessConfig]:
    return load_all_configs()


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(ProcessId.TEA)


@pytest.fixture()
def ctx(tea_config) -> AgentContext:
    return AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-1",
    )


def _agent(configs: dict[ProcessId, ProcessConfig], results: list[ModelResponse | Exception]):
    gateway = _FakeGateway(results)
    return TriageAgent(cast(ModelGateway, gateway), configs), gateway


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _TEA_MESSAGE}],
    }
    state.update(overrides)
    return state


# ---- 第一类：分流 ----


async def test_tea_question_routes_to_tea(configs, ctx) -> None:
    """输入"我家茶树叶子发黄"分流到 tea，且农户原话进了提示词。"""
    agent, gateway = _agent(configs, [_ok()])
    result = await agent.run(_state(), ctx)

    assert result.process_id is ProcessId.TEA
    user_content = gateway.last_messages[-1]["content"]
    assert _TEA_MESSAGE in user_content
    assert agent.task_type is TaskType.TRIAGE


async def test_system_prompt_lists_all_seven_processes(configs, ctx) -> None:
    """系统提示包含全部七个 `process_id` 与各自流程的展示名。"""
    agent, gateway = _agent(configs, [_ok()])
    await agent.run(_state(), ctx)

    system_content = gateway.last_messages[0]["content"]
    for pid in ProcessId:
        assert pid.value in system_content
        assert configs[pid].display_name in system_content
    assert "不得生成任何问题" in system_content


def test_triage_contract_has_no_question_field() -> None:
    """硬约束的契约侧保证：`TriageResult` 里没有承载问题的字段。"""
    forbidden = ("question", "prompt", "ask")
    for name in TriageResult.model_fields:
        assert not any(token in name.lower() for token in forbidden), name


# ---- 第二类：对象确认 ----


async def test_profile_rice_but_asking_tea_overrides_object_confirmed(configs, ctx) -> None:
    """档案为水稻而农户问茶时，`object_confirmed` 被覆写为 False。"""
    agent, _ = _agent(configs, [_ok(object_confirmed=True)])
    state = _state(profile_process_id="rice")

    result = await agent.run(state, ctx)

    assert result.process_id is ProcessId.TEA
    assert result.object_confirmed is False


async def test_matching_profile_needs_no_confirmation(configs, ctx) -> None:
    """档案与表述一致时不做确认，模型给的 `object_confirmed=True` 保留。"""
    agent, _ = _agent(configs, [_ok()])
    state = _state(profile_process_id="tea")

    result = await agent.run(state, ctx)

    assert result.object_confirmed is True


def test_resolve_object_confirmation_returns_none_without_profile(configs, tea_config) -> None:
    """档案为空时返回 `None`：没有可比对的对象，问也无从问起。"""
    agent = TriageAgent(cast(ModelGateway, _FakeGateway([_ok()])), configs)
    triage = TriageResult.model_validate(_payload())

    assert agent.resolve_object_confirmation(triage, _state()) is None
    assert agent.resolve_object_confirmation(triage, _state(profile_process_id=None)) is None


def test_resolve_object_confirmation_builds_request_when_conflicting(configs) -> None:
    """冲突时给出的确认请求带上了档案与本次问的两侧标签。"""
    agent = TriageAgent(cast(ModelGateway, _FakeGateway([_ok()])), configs)
    triage = TriageResult.model_validate(_payload())

    request = agent.resolve_object_confirmation(triage, _state(profile_process_id="rice"))

    assert request is not None
    assert request.profile_process_id is ProcessId.RICE
    assert request.asked_process_id is ProcessId.TEA
    assert request.candidate_labels == ["水稻", "茶树"]
    assert "水稻" in request.question_text and "茶树" in request.question_text


# ---- 第三类：降级 ----


async def test_gateway_failure_degrades_to_general(configs, ctx) -> None:
    """网关失败时降级为 general，`fallback_reason` 非空且带上失败原因。"""
    agent, gateway = _agent(configs, [ValueError("网关未装配")])

    result = await agent.run(_state(trace_id="t-9"), ctx)

    assert len(gateway.calls) == 1
    assert result.process_id is ProcessId.GENERAL
    assert result.fallback_reason and "ValueError" in result.fallback_reason
    # 降级到 general 无需确认对象：再插一次确认交互对农户是纯粹的额外负担。
    assert result.object_confirmed is True
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "triage", "mode": "degrade"}) == 1


async def test_general_without_reason_gets_backfilled(configs, tea_config) -> None:
    """模型给出 general 却给了空原因时，`post_validate` 补填，避免契约字段为空。

    契约的校验器会拦下 `general` 无原因的构造，因此这里用 `model_construct` 直接造出
    "字段为空串"的边界值，模拟模型真的这么输出时 `post_validate` 的行为。
    """
    agent, _ = _agent(configs, [_ok()])
    ctx = AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-1",
    )
    raw = TriageResult.model_construct(
        process_id=ProcessId.GENERAL,
        object_confirmed=True,
        object_label="未指定作物",
        slot_gaps=[],
        profile_ready=False,
        fallback_reason="",
        handoff_to_report=False,
    )

    result = await agent.post_validate(raw, _state(), ctx)

    assert result.process_id is ProcessId.GENERAL
    assert result.fallback_reason


# ---- 槽位缺口 ----


def test_slot_gaps_exclude_auto_fillable_slots(configs, tea_config) -> None:
    """`source_hint` 非空的槽位不纳入缺口；缺口只含必问且需人工回答的槽位。"""
    agent = TriageAgent(cast(ModelGateway, _FakeGateway([_ok()])), configs)

    gaps = agent.compute_slot_gaps(_state(), tea_config)

    auto_fillable = {s.key for s in tea_config.required_slots if s.source_hint}
    assert auto_fillable, "样例流程应至少有一个可自动填充的槽位，否则本用例失去意义"
    assert not auto_fillable & set(gaps)
    assert set(gaps) == {"tea_symptom_part", "tea_symptom_pattern", "tea_symptom_scope"}


def test_slot_gaps_ignore_filled_and_unknown(configs, tea_config) -> None:
    """已确认与"问过但答不出"都不算缺口：`unknown` 是合法答案。"""
    agent = TriageAgent(cast(ModelGateway, _FakeGateway([_ok()])), configs)
    slots = {
        "tea_symptom_part": SlotValue(key="tea_symptom_part", value="叶片", status="filled", source="user_answer"),
        "tea_symptom_pattern": SlotValue(key="tea_symptom_pattern", status="unknown", source="user_answer"),
    }

    gaps = agent.compute_slot_gaps(_state(slots=slots), tea_config)

    assert gaps == ["tea_symptom_scope"]


async def test_model_slot_gaps_are_overwritten_by_code(configs, ctx) -> None:
    """模型给的 `slot_gaps` 被代码计算结果覆盖。"""
    agent, _ = _agent(configs, [_ok(slot_gaps=["模型编的槽位"])])

    result = await agent.run(_state(), ctx)

    assert result.slot_gaps == ["tea_symptom_part", "tea_symptom_pattern", "tea_symptom_scope"]


# ---- mock 模式与节点入口 ----


async def test_run_under_mock_returns_process_id_enum() -> None:
    """`USE_MOCK_LLM=true` 下 `run` 返回的 `process_id` 是枚举实例而非字符串。"""
    agent = TriageAgent(build_gateway(), load_all_configs())
    ctx = AgentContext(
        process_config=get_process_config(ProcessId.GENERAL),
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-mock",
    )

    result = await agent.run(_state(), ctx)

    assert isinstance(result.process_id, ProcessId)
    assert type(result.process_id) is ProcessId


async def test_run_triage_node_writes_state_keys() -> None:
    """主图节点返回 `triage` / `process_id` / `stage` 三个增量键。"""
    update = await run_triage(_state())

    assert set(update) == {"triage", "process_id", "stage"}
    assert isinstance(update["process_id"], ProcessId)
    assert TriageResult.model_validate(update["triage"]).process_id is update["process_id"]


async def test_no_network_request_during_run(configs, ctx, respx_mock: respx.Router) -> None:
    """分诊全程不得发起任何 HTTP 请求。"""
    agent, gateway = _agent(configs, [_ok()])

    await agent.run(_state(), ctx)

    assert len(gateway.calls) == 1
    assert respx_mock.calls == []
