"""T2-13：智能体基类的重试、降级与 fail-closed 分派。

这六类用例对应基类承诺的六条行为：首次成功不重试、契约违约重试后降级、业务异常
不重试直接降级、`FAIL_CLOSED` 走 fail_closed、`fail_closed` 未实现必须显式报错、
预算紧张时不再重试。用例全部用替身网关而不是 `respx` 注册响应：网关在这一层是
**被调用方**，它内部走 HTTP 还是走 mock provider 与基类行为无关；再加一条 `respx`
零请求断言，保证基类不会绕过网关自己发请求。
"""

import json
import time
from typing import Any, cast

import pytest
import respx

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.agents.exceptions import ContractViolation
from app.agent.contracts.triage import TriageResult
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.llm.errors import TransientModelError
from app.llm.gateway import ModelGateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config import ProcessConfig
from app.process_config.loader import load_process_config

_VALID_TRIAGE: dict[str, Any] = {
    "process_id": "tea",
    "object_confirmed": True,
    "object_label": "茶树·2亩",
    "slot_gaps": [],
    "profile_ready": True,
}


def _ok(payload: dict[str, Any] | None = None) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象（适配器负责校验），不是裸字典。"""
    data = dict(_VALID_TRIAGE) if payload is None else payload
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=TriageResult.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


def _unparsable() -> ModelResponse:
    """网关对解析失败的处理：`parsed=None` + `error`，而不是抛异常。"""
    return ModelResponse(
        content="{ 截断的 JSON",
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
    def message_lengths(self) -> list[int]:
        return [len(messages) for _, messages, _ in self.calls]


class _TriageAgent(BaseAgent[TriageResult]):
    """最简具体智能体：只声明契约、提示词与降级出口。"""

    name = "triage"
    output_schema = TriageResult
    task_type = TaskType.TRIAGE

    def __init__(self, gateway: ModelGateway, *, validate_failures: int = 0) -> None:
        super().__init__(gateway)
        self._validate_failures = validate_failures
        self.degrade_reasons: list[str] = []
        self.post_validated: list[TriageResult] = []

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        return [
            {"role": "system", "content": "你是分诊智能体"},
            {"role": "user", "content": "叶子枯了"},
        ]

    def validate_output(self, result: TriageResult, state: ConsultationState, ctx: AgentContext) -> None:
        if self._validate_failures > 0:
            self._validate_failures -= 1
            raise ContractViolation("slot_gaps 与流程不匹配")

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> TriageResult:
        self.degrade_reasons.append(reason)
        return TriageResult(
            process_id=ProcessId.GENERAL,
            object_confirmed=False,
            object_label="未识别",
            slot_gaps=[],
            profile_ready=False,
            fallback_reason=reason,
        )

    async def post_validate(self, result: TriageResult, state: ConsultationState, ctx: AgentContext) -> TriageResult:
        self.post_validated.append(result)
        return result


class _GuardAgent(BaseAgent[TriageResult]):
    """守卫类：不确定时报，而不是降级继续。"""

    name = "report_guard"
    output_schema = TriageResult
    task_type = TaskType.REPORT_GUARD
    failure_mode = FailureMode.FAIL_CLOSED

    def __init__(self, gateway: ModelGateway) -> None:
        super().__init__(gateway)
        self.closed_reasons: list[str] = []

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        return [{"role": "system", "content": "你是报告守卫"}]

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> TriageResult:
        raise AssertionError("FAIL_CLOSED 智能体不得走 degrade 路径")

    async def fail_closed(self, state: ConsultationState, ctx: AgentContext, reason: str) -> TriageResult:
        self.closed_reasons.append(reason)
        return TriageResult(
            process_id=ProcessId.TEA,
            object_confirmed=False,
            object_label="待上报",
            slot_gaps=[],
            profile_ready=False,
        )


class _UnimplementedGuardAgent(BaseAgent[TriageResult]):
    """声明了 fail_closed 却没实现：基类必须让它显式失败，而不是给个假保守值。"""

    name = "unimplemented_guard"
    output_schema = TriageResult
    task_type = TaskType.REPORT_GUARD
    failure_mode = FailureMode.FAIL_CLOSED

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        return [{"role": "system", "content": "你是报告守卫"}]

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> TriageResult:
        raise AssertionError("FAIL_CLOSED 智能体不得走 degrade 路径")


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return load_process_config(ProcessId.TEA)


@pytest.fixture()
def state() -> ConsultationState:
    return {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "process_id": ProcessId.TEA,
        "round": 1,
        "slot_round": 0,
    }


@pytest.fixture()
def ctx(tea_config) -> AgentContext:
    return AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-1",
    )


def _agent(results: list[ModelResponse | Exception], **kwargs) -> tuple[_TriageAgent, _FakeGateway]:
    gateway = _FakeGateway(results)
    return _TriageAgent(cast(ModelGateway, gateway), **kwargs), gateway


async def test_first_attempt_success_does_not_retry(state, ctx) -> None:
    """首次即成功：只调一次，且不进降级。"""
    agent, gateway = _agent([_ok()])
    result = await agent.run(state, ctx)

    assert len(gateway.calls) == 1
    assert result.process_id is ProcessId.TEA
    assert agent.degrade_reasons == []
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "triage", "mode": "degrade"}) == 0


async def test_contract_violation_retries_then_degrades(state, ctx) -> None:
    """契约违约：重试到上限后降级，且降级产出仍过 post_validate。"""
    agent, gateway = _agent([_unparsable()])
    started = time.monotonic()
    result = await agent.run(state, ctx)
    elapsed = time.monotonic() - started

    # 首次 + 2 次重试，恰好 3 次调用，不超出验收上限。
    assert len(gateway.calls) == 3
    # 退避 0.5 + 1.0 秒（第三次尝试前的等待为 1 秒），上限 4 秒。
    assert 1.4 <= elapsed < 4.0

    # 降级出口的类型与 output_schema 一致，且带上了失败原因。
    assert isinstance(result, TriageResult)
    assert result.process_id is ProcessId.GENERAL
    assert result.fallback_reason and "ContractViolation" in result.fallback_reason
    # 降级产出被 post_validate 再次校验（同一个对象，未复制）。
    assert agent.post_validated == [result]

    # 降级只上报一次，标签同时含 agent 与 mode。
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "triage", "mode": "degrade"}) == 1


async def test_repair_hint_appended_from_second_attempt(state, ctx) -> None:
    """修复提示从第二次尝试起追加两条消息，第一次不追加。"""
    agent, gateway = _agent([_unparsable()])
    await agent.run(state, ctx)

    lengths = gateway.message_lengths
    assert lengths[0] == 2  # 系统 + 用户
    assert lengths[1] == 4  # 多出 assistant 说明与 user 修复要求
    assert lengths[2] == 4
    assert gateway.calls[0][1][0]["role"] == "system"


async def test_transient_error_is_retried_then_succeeds(state, ctx) -> None:
    """瞬时故障属可重试类：第二次成功即返回，不进降级。"""
    agent, gateway = _agent([TransientModelError("超时"), _ok()])
    started = time.monotonic()
    result = await agent.run(state, ctx)
    elapsed = time.monotonic() - started

    assert len(gateway.calls) == 2
    assert 0.4 <= elapsed < 3.0
    assert result.process_id is ProcessId.TEA
    assert agent.degrade_reasons == []


async def test_business_error_degrades_without_retry(state, ctx) -> None:
    """业务异常重试不会成功：只调一次，直接降级。"""
    agent, gateway = _agent([ValueError("流程配置缺失")])
    result = await agent.run(state, ctx)

    assert len(gateway.calls) == 1
    assert result.process_id is ProcessId.GENERAL
    assert agent.degrade_reasons and "ValueError" in agent.degrade_reasons[0]


async def test_validate_output_violation_retries_then_succeeds(state, ctx) -> None:
    """`validate_output` 抛契约违约同样触发重试；重试成功后不降级。"""
    agent, gateway = _agent([_ok(), _ok()], validate_failures=1)
    result = await agent.run(state, ctx)

    assert len(gateway.calls) == 2
    assert result.process_id is ProcessId.TEA
    assert agent.degrade_reasons == []


async def test_fail_closed_agent_uses_fail_closed_path(tea_config, state) -> None:
    """守卫类失败走 `fail_closed`，绝不走 `degrade`。"""
    gateway = _FakeGateway([_unparsable()])
    agent = _GuardAgent(cast(ModelGateway, gateway))
    ctx = AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.REPORT_GUARD,
        trace_id="t-2",
    )
    result = await agent.run(state, ctx)

    assert result.process_id is ProcessId.TEA
    assert agent.closed_reasons and "ContractViolation" in agent.closed_reasons[0]
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "report_guard", "mode": "fail_closed"}) == 1


async def test_fail_closed_not_implemented_raises(tea_config, state) -> None:
    """`fail_closed` 未实现时抛 NotImplementedError，而不是给一个假保守值。"""
    gateway = _FakeGateway([_unparsable()])
    agent = _UnimplementedGuardAgent(cast(ModelGateway, gateway))
    ctx = AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.REPORT_GUARD,
        trace_id="t-3",
    )

    with pytest.raises(NotImplementedError, match="未实现"):
        await agent.run(state, ctx)


async def test_low_budget_reduces_attempts(state, tea_config) -> None:
    """预算低于阈值时 `effective_max_attempts` 降为 1：总调用 2 次而非 3 次。"""
    agent, gateway = _agent([_unparsable()])
    ctx = AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-4",
        budget_remaining_tokens=1000,
    )

    assert agent.effective_max_attempts(ctx) == 1
    result = await agent.run(state, ctx)

    assert len(gateway.calls) == 2
    assert result.process_id is ProcessId.GENERAL


async def test_no_network_request_during_run(state, ctx, respx_mock: respx.Router) -> None:
    """`USE_MOCK_LLM=true` 下基类不得自行发起任何 HTTP 请求。"""
    agent, gateway = _agent([_unparsable()])
    await agent.run(state, ctx)

    assert len(gateway.calls) == 3
    assert respx_mock.calls == []
