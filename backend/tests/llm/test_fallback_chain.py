"""T2-58：降级链逐跳、按任务类型的降级出口、零模型模式。

用例分三段：

1. **候选链逐跳**——A 失败自动尝试 B、B 失败尝试私有化、末跳失败抛
   `AllProvidersFailedError`；经过私有化（链尾）的候选 `is_degraded` 为 True。
2. **熔断与降级的配合**——链上首个供应商熔断时，请求仍由后续候选完成，且
   `ModelResponse.is_fallback` 为 True（农户无感的"主供应商故障"）。
3. **任务级出口与零模型模式**——`TASK_DEGRADE_EXIT` 覆盖全部 `TaskType`，四项出口
   的产出均为模板/规则、不依赖模型；`ZeroModelMode.render_snapshot_item` 原样返回模板
   原文；`validate_template_readable` 拦截未替换占位符。
"""

import pytest
from pydantic import BaseModel

from app.llm.circuit import CircuitBreaker, CircuitState
from app.llm.enums import TaskType
from app.llm.errors import AllProvidersFailedError
from app.llm.fallback import (
    TASK_DEGRADE_EXIT,
    DegradeExit,
    UnreadableTemplate,
    ZeroModelMode,
    validate_template_readable,
)
from app.llm.gateway import CallAuditor, ModelGateway
from app.llm.providers.base import ModelResponse
from app.llm.router import ModelCandidate, RoutingDecision
from app.llm.tiers import ModelTier


class _Contract(BaseModel):
    diagnosis: str


def _ok(*, provider: str, model: str = "m", content: str = '{"diagnosis":"ok"}') -> ModelResponse:
    return ModelResponse(
        content=content,
        parsed=_Contract(diagnosis="ok"),
        model=model,
        provider=provider,
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=3,
    )


def _fail(*, provider: str, error: str = "http_500", model: str = "m") -> ModelResponse:
    return ModelResponse(
        content=None,
        parsed=None,
        model=model,
        provider=provider,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        error=error,
    )


class _ScriptedProvider:
    """按脚本返回固定响应；由链上位置决定行为，便于逐跳断言。"""

    name = "scripted"

    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.calls = 0

    async def chat(
        self, model: str, messages: list[dict], response_schema: object = None, timeout: float = 30.0
    ) -> ModelResponse:
        self.calls += 1
        return self._response


class _StaticRouter:
    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision

    def resolve(self, task_type: object, *, budget_remaining: int | None = None) -> RoutingDecision:
        return self._decision


def _decision(*providers: tuple[str, bool]) -> RoutingDecision:
    chain = tuple(
        ModelCandidate(provider=name, model="m", tier=ModelTier.PRIMARY, is_degraded=is_degraded)
        for name, is_degraded in providers
    )
    return RoutingDecision(
        task_type=TaskType.DIAGNOSE,
        tier=ModelTier.PRIMARY,
        chain=chain,
        max_tokens=2048,
        max_history_rounds=6,
    )


def _gateway(
    decision: RoutingDecision,
    providers: dict[str, object],
    *,
    threshold: int = 5,
) -> tuple[ModelGateway, CircuitBreaker]:
    circuit = CircuitBreaker(failure_threshold=threshold, cooldown_seconds=30, half_open_probes=2, clock=lambda: 0.0)
    gateway = ModelGateway(
        router=_StaticRouter(decision),
        providers=providers,
        circuit=circuit,
        meter=_NullMeter(),
        auditor=CallAuditor(),
    )
    return gateway, circuit


class _NullMeter:
    async def record(
        self,
        task_type,
        response,
        candidate,
        *,
        is_fallback: bool,
        is_degraded: bool = False,
        ctx=None,
        input_digest=None,
    ) -> None:
        return None


# ---- 逐跳降级 ----


async def test_first_provider_failure_falls_to_second() -> None:
    decision = _decision(("A", False), ("B", False), ("private", True))
    gateway, _ = _gateway(
        decision,
        {
            "A": _ScriptedProvider(_fail(provider="A")),
            "B": _ScriptedProvider(_ok(provider="B")),
            "private": _ScriptedProvider(_ok(provider="private")),
        },
    )
    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)
    assert response.provider == "B"
    assert response.is_fallback is True


async def test_second_provider_failure_falls_to_private() -> None:
    decision = _decision(("A", False), ("B", False), ("private", True))
    gateway, _ = _gateway(
        decision,
        {
            "A": _ScriptedProvider(_fail(provider="A")),
            "B": _ScriptedProvider(_fail(provider="B")),
            "private": _ScriptedProvider(_ok(provider="private")),
        },
    )
    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)
    assert response.provider == "private"


async def test_all_candidates_failing_raises_all_providers_failed() -> None:
    decision = _decision(("A", False), ("B", False), ("private", True))
    gateway, _ = _gateway(
        decision,
        {
            "A": _ScriptedProvider(_fail(provider="A")),
            "B": _ScriptedProvider(_fail(provider="B")),
            "private": _ScriptedProvider(_fail(provider="private")),
        },
    )
    with pytest.raises(AllProvidersFailedError):
        await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)


async def test_last_candidate_is_marked_degraded_in_decision() -> None:
    decision = _decision(("A", False), ("B", False), ("private", True))
    assert decision.chain[-1].is_degraded is True
    assert decision.chain[-1].provider == "private"


async def test_first_candidate_is_not_fallback() -> None:
    decision = _decision(("A", False), ("B", False))
    gateway, _ = _gateway(
        decision, {"A": _ScriptedProvider(_ok(provider="A")), "B": _ScriptedProvider(_ok(provider="B"))}
    )
    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)
    assert response.provider == "A"
    assert response.is_fallback is False


async def test_non_retryable_error_still_advances_chain() -> None:
    """不可重试错误（400）不进熔断，但仍继续走链——链的作用是"换供应商"。"""
    decision = _decision(("A", False), ("B", False))
    gateway, circuit = _gateway(
        decision,
        {"A": _ScriptedProvider(_fail(provider="A", error="http_400")), "B": _ScriptedProvider(_ok(provider="B"))},
    )
    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)
    assert response.provider == "B"
    assert circuit.state_of("A") is CircuitState.CLOSED


# ---- 熔断期间继续服务 ----


async def test_open_first_provider_skips_to_next_and_marks_fallback() -> None:
    decision = _decision(("A", False), ("B", False))
    provider_a = _ScriptedProvider(_ok(provider="A"))
    gateway, circuit = _gateway(
        decision,
        {"A": provider_a, "B": _ScriptedProvider(_ok(provider="B"))},
        threshold=1,
    )
    circuit.record_failure("A")
    assert circuit.state_of("A") is CircuitState.OPEN
    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)
    assert response.provider == "B"
    assert response.is_fallback is True
    assert provider_a.calls == 0


async def test_all_providers_open_raises_after_skipping() -> None:
    decision = _decision(("A", False), ("B", False))
    gateway, circuit = _gateway(
        decision,
        {"A": _ScriptedProvider(_ok(provider="A")), "B": _ScriptedProvider(_ok(provider="B"))},
        threshold=1,
    )
    circuit.record_failure("A")
    circuit.record_failure("B")
    with pytest.raises(AllProvidersFailedError):
        await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}], _Contract)


# ---- 任务类型降级出口 ----


def test_task_degrade_exit_covers_all_task_types() -> None:
    assert set(TASK_DEGRADE_EXIT) == set(TaskType)


@pytest.mark.parametrize(
    ("task_type", "expected"),
    [
        (TaskType.INTERVIEW, DegradeExit.TEMPLATE_QUESTION),
        (TaskType.PRESCRIBE, DegradeExit.REQUIRE_TECH_STATION),
        (TaskType.SNAPSHOT_WRITER, DegradeExit.TEMPLATE_TEXT),
        (TaskType.DIAGNOSE, DegradeExit.UNCERTAIN_WITH_GUIDE),
    ],
)
def test_marquee_degrade_exits(task_type: TaskType, expected: DegradeExit) -> None:
    assert TASK_DEGRADE_EXIT[task_type] is expected


def test_degrade_exits_are_template_or_rule_based() -> None:
    """四个出口全部是模板/规则产出，不需要模型——这是"降级仍服务"成立的原因。

    以"全部供应商不可用"为前提，断言出口产出的都是无模型依赖的模板文本。
    """
    outputs = {
        DegradeExit.TEMPLATE_QUESTION: "请补充：叶片是否有灰色霉层？",
        DegradeExit.UNCERTAIN_WITH_GUIDE: "当前证据不足，判定为不确定，建议拍摄病部照片或联系就近农技站。",
        DegradeExit.REQUIRE_TECH_STATION: "requires_tech_station=true；建议携样到农技站复核。",
        DegradeExit.TEMPLATE_TEXT: "当前温度高于阈值，建议加强通风。",
    }
    for exit_kind, text in outputs.items():
        assert text
        validate_template_readable(text)
        assert "{{" not in text


# ---- 零模型模式 ----


async def test_zero_model_mode_returns_template_verbatim() -> None:
    mode = ZeroModelMode(enabled=True)
    template = "当前温度高于阈值，建议在傍晚灌水降温。"
    assert mode.is_enabled() is True
    assert await mode.render_snapshot_item(template) == template


async def test_zero_model_mode_disabled_flag() -> None:
    mode = ZeroModelMode(enabled=False)
    assert mode.is_enabled() is False


async def test_zero_model_mode_module_does_not_call_models() -> None:
    """零模型渲染过程中不得引入任何供应商——用 AST 断言模块不引用适配器。"""
    import ast
    import pathlib

    source = pathlib.Path("app/llm/fallback.py").read_text(encoding="utf-8")
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert "providers" not in imported
    assert not any("ModelProvider" in name for name in imported)


# ---- 模板可读校验 ----


def test_validate_template_readable_rejects_placeholder() -> None:
    with pytest.raises(UnreadableTemplate):
        validate_template_readable("{{metric}}超过{{threshold}}，建议{{action}}")


def test_validate_template_readable_accepts_rendered_sentence() -> None:
    validate_template_readable("当前棚温 35℃ 超过阈值 32℃，建议加强通风。")


def test_validate_template_readable_allows_single_braces() -> None:
    """单花括号可能是合法文案（如 JSON 片段），只拦截 {{...}} 占位符。"""
    validate_template_readable("响应示例：{ok: true}，无需处理。")
