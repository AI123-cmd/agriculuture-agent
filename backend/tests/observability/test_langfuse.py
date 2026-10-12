"""T6-07：Langfuse 私有化上报的接入面与弱依赖性质。

不真连 Langfuse（那要起一套 Compose 栈，且与"上报写的对不对"无关）。被测的是三件事：

1. **开关语义**——`LANGFUSE_ENABLED=false` 时不构造任何客户端，上游零请求。
2. **弱依赖**——开关打开但 Langfuse 不可达时，业务照常完成，导出失败被丢弃。
3. **属性完整性**——generation span 上带 `prompt.name`/`prompt.version`/`prompt.is_fallback`
   三属性，且版本号与 T2-60 注册表（`app.agent.prompts.registry`）当前值一致。

另两条与抽样有关：`LANGFUSE_TRACE_RATIO` 越界值夹紧；抽中的 trace 从根开始、
不出现"父在子不在"的残缺 trace（这是 `ParentBased` 的意义所在）。

属性断言基于 `InMemorySpanExporter`：它把已结束的 span 留在内存里，能直接查名称与
属性；生产走 OTLP 批量导出，形态与本文件无关。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agent.prompts.registry import current_prompt_version
from app.llm import ModelGateway, TaskType
from app.llm.circuit import CircuitBreaker
from app.llm.gateway import CallAuditor
from app.llm.providers.base import ModelResponse
from app.llm.router import ModelCandidate, RoutingDecision
from app.llm.tiers import ModelTier
from app.observability import (
    GENERATION_SPAN_NAME,
    ROOT_SPAN_NAME,
    configure_tracing,
    consultation_span,
    force_flush,
    generation_span,
    record_prompt_identity,
    reset_tracing,
)
from app.observability.attributes import PROMPT_IS_FALLBACK, PROMPT_NAME, PROMPT_VERSION
from app.observability.langfuse import build_langfuse_exporter
from app.observability.prompts import prompt_name_of, prompt_version_of
from app.observability.tracing import spans_of


def _settings(**overrides: Any) -> SimpleNamespace:
    """配置替身：`configure_tracing` 与导出器只按属性名取值，不需要完整 `Settings`。"""
    base = {
        "OTEL_ENABLED": True,
        "OTEL_EXPORTER": "langfuse",
        "OTEL_ENDPOINT": "",
        "OTEL_SERVICE_NAME": "huanong-test",
        "LANGFUSE_ENABLED": False,
        "LANGFUSE_HOST": "",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "LANGFUSE_TRACE_RATIO": 1.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    """注入内存导出器并重置模块自持的 provider 引用，用例之间不串状态。"""
    exp = InMemorySpanExporter()
    configure_tracing(_settings(), exporter=exp)
    yield exp
    reset_tracing()


# ---- 1. 构造与开关语义 ----


def test_disabled_returns_no_exporter() -> None:
    """`LANGFUSE_ENABLED=false`：即便地址与密钥齐全也不构造客户端，上游零请求。"""
    exp = build_langfuse_exporter(
        _settings(
            LANGFUSE_ENABLED=False,
            LANGFUSE_HOST="http://langfuse.local",
            LANGFUSE_PUBLIC_KEY="pk",
            LANGFUSE_SECRET_KEY="sk",
        )
    )
    assert exp is None


def test_incomplete_config_returns_no_exporter() -> None:
    """地址缺失或密钥缺失：判定为不可用，返回 `None` 而不是抛错（观测不得阻断启动）。"""
    assert build_langfuse_exporter(_settings(LANGFUSE_ENABLED=True)) is None
    assert build_langfuse_exporter(_settings(LANGFUSE_ENABLED=True, LANGFUSE_HOST="http://langfuse.local")) is None


async def test_disabled_makes_no_upstream_request(respx_mock: respx.Router) -> None:
    """关闭时注册的是空操作 provider：跑一段链路也不产生任何上游请求。"""
    try:
        provider = configure_tracing(
            _settings(
                LANGFUSE_ENABLED=False,
                LANGFUSE_HOST="http://langfuse.local",
                LANGFUSE_PUBLIC_KEY="pk",
                LANGFUSE_SECRET_KEY="sk",
            )
        )
        assert provider is None
        with consultation_span("sess-1", "rice"), generation_span("diagnose"):
            pass
        force_flush()
        assert respx_mock.calls == []
    finally:
        reset_tracing()


def test_trace_ratio_clamps_out_of_range() -> None:
    """越界抽样比静默夹紧到 `[0, 1]`：`2.0` 是笔误，不该让"全量上报"变成不可解释行为。"""
    cases = [(2.0, 1.0), (-1.0, 0.0), (0.2, 0.2)]
    for raw, expected in cases:
        exp = build_langfuse_exporter(
            _settings(
                LANGFUSE_ENABLED=True,
                LANGFUSE_HOST="http://langfuse.test",
                LANGFUSE_PUBLIC_KEY="pk",
                LANGFUSE_SECRET_KEY="sk",
                LANGFUSE_TRACE_RATIO=raw,
            )
        )
        assert exp is not None
        assert exp.trace_ratio() == expected


# ---- 2. 弱依赖：Langfuse 不可达不影响业务 ----


async def test_unreachable_langfuse_does_not_fail_run() -> None:
    """开关打开但端点连不上：导出失败被丢弃，业务链路照常完成。"""
    try:
        # 端点指向必然连不上的本地端口；构造不建连接，失败发生在导出时并被批量导出器吞掉。
        configure_tracing(
            _settings(
                LANGFUSE_ENABLED=True,
                LANGFUSE_HOST="http://127.0.0.1:1",
                LANGFUSE_PUBLIC_KEY="pk",
                LANGFUSE_SECRET_KEY="sk",
            )
        )
        done: dict[str, bool] = {}
        with consultation_span("sess-1", "rice"), generation_span("diagnose"):
            done["ok"] = True
        force_flush(timeout_millis=100)
        assert done["ok"] is True
    finally:
        reset_tracing()


# ---- 3. 提示词身份：解析与写入 ----


def test_prompt_name_maps_task_type_to_registry_template() -> None:
    """任务类型映射到注册表模板名；未登记的映射回退为任务类型本身。"""
    assert prompt_name_of("interview") == "interviewer"
    assert prompt_name_of("diagnose") == "diagnoser"
    assert prompt_name_of("brand_new") == "brand_new"


def test_prompt_version_matches_registry() -> None:
    """版本号来源唯一：与 T2-60 注册表当前值一致；未登记模板名取 0 而不抛错。"""
    assert prompt_version_of("diagnoser") == current_prompt_version("diagnoser")
    assert prompt_version_of("interviewer") == current_prompt_version("interviewer")
    assert prompt_version_of("never_registered") == 0


def test_record_prompt_identity_writes_three_attributes() -> None:
    """三属性必须同时写入：缺任一都会让"环节 + 版本"聚合无法成立。"""
    exp = InMemorySpanExporter()
    try:
        configure_tracing(_settings(), exporter=exp)
        with generation_span("diagnose") as span:
            record_prompt_identity(span, "diagnose", is_fallback=True)
        force_flush()
        gen = spans_of(exp)[0]
    finally:
        reset_tracing()

    assert gen.attributes[PROMPT_NAME] == "diagnoser"
    assert gen.attributes[PROMPT_VERSION] == current_prompt_version("diagnoser")
    assert gen.attributes[PROMPT_IS_FALLBACK] is True


# ---- 4. 网关接入：真实调用链上写出的三属性 ----


class _StubRouter:
    """返回固定决策的路由替身。"""

    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision

    def resolve(self, task_type: object, *, budget_remaining: int | None = None) -> RoutingDecision:
        return self._decision


class _StubProvider:
    """回放固定响应的适配器替身。"""

    def __init__(self, name: str, response: ModelResponse) -> None:
        self.name = name
        self._response = response

    async def chat(
        self,
        model: str,
        messages: list[dict],
        response_schema: object = None,
        timeout: float = 30.0,
    ) -> ModelResponse:
        return self._response.model_copy()


class _NullMeter:
    """不落库的计量替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def record(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def _response(*, provider: str, error: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=None if error else "ok",
        parsed=None,
        model=f"model-{provider}",
        provider=provider,
        prompt_tokens=1,
        completion_tokens=2,
        latency_ms=3,
        error=error,
    )


def _gateway(task_type: TaskType, providers: list[str], responses: dict[str, ModelResponse]) -> ModelGateway:
    chain = tuple(ModelCandidate(provider=p, model=f"model-{p}", tier=ModelTier.SMALL) for p in providers)
    decision = RoutingDecision(
        task_type=task_type,
        tier=ModelTier.SMALL,
        chain=chain,
        max_tokens=1024,
        max_history_rounds=6,
    )
    instances = {p: _StubProvider(p, responses[p]) for p in providers}
    circuit = CircuitBreaker(failure_threshold=5, cooldown_seconds=30, half_open_probes=2, clock=lambda: 0.0)
    return ModelGateway(
        router=_StubRouter(decision),
        providers=instances,
        circuit=circuit,
        meter=_NullMeter(),
        auditor=CallAuditor(),
    )


async def test_gateway_generation_span_carries_prompt_identity(exporter: InMemorySpanExporter) -> None:
    """网关发起的模型调用：generation span 上带提示词三属性，版本与注册表一致。"""
    gateway = _gateway(TaskType.DIAGNOSE, ["p1"], {"p1": _response(provider="p1")})
    with consultation_span("sess-1", "rice"):
        await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "叶片发黄"}])
    force_flush()

    gen = next(s for s in spans_of(exporter) if s.name == GENERATION_SPAN_NAME)
    assert gen.attributes[PROMPT_NAME] == "diagnoser"
    assert gen.attributes[PROMPT_VERSION] == current_prompt_version("diagnoser")
    assert gen.attributes[PROMPT_IS_FALLBACK] is False


async def test_gateway_marks_fallback_candidate(exporter: InMemorySpanExporter) -> None:
    """首选失败、回退到备选：`prompt.is_fallback=True`，让降档调用在观测侧可区分。"""
    gateway = _gateway(
        TaskType.DIAGNOSE,
        ["p1", "p2"],
        {"p1": _response(provider="p1", error="http_500"), "p2": _response(provider="p2")},
    )
    with consultation_span("sess-1", "rice"):
        await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "叶片发黄"}])
    force_flush()

    # 两次候选调用各有一个 generation span，回退那次标了 is_fallback。
    generations = [s for s in spans_of(exporter) if s.name == GENERATION_SPAN_NAME]
    assert len(generations) == 2
    assert [s.attributes[PROMPT_IS_FALLBACK] for s in generations] == [False, True]


# ---- 5. 抽样：只发生在根 span，不产生残缺 trace ----


async def test_trace_ratio_samples_root_and_keeps_trace_complete() -> None:
    """`LANGFUSE_TRACE_RATIO=0.2` 跑 100 条：抽中 10~30 条，且每条都从根开始。"""
    exp = InMemorySpanExporter()
    try:
        configure_tracing(_settings(LANGFUSE_TRACE_RATIO=0.2), exporter=exp)
        for _ in range(100):
            with consultation_span("sess", "rice"), generation_span("diagnose"):
                pass
        force_flush()
        spans = spans_of(exp)
    finally:
        reset_tracing()

    roots = [s for s in spans if s.name == ROOT_SPAN_NAME]
    assert 10 <= len(roots) <= 30

    root_traces = {format(s.context.trace_id, "032x") for s in roots}
    child_traces = {
        format(s.context.trace_id, "032x") for s in spans if s.name == GENERATION_SPAN_NAME
    }
    # 子 span 的 trace 必然是已抽中的 trace；每条抽中的 trace 也都有根与子 span。
    assert child_traces <= root_traces
    assert child_traces == root_traces
