"""OpenTelemetry 链路追踪（T6-06，技术方案 14.4）。

一次问诊 = 一个 trace，每个智能体 = 一个 span，每次模型调用 = 一个 generation。
三层嵌套由 OTel 的"当前 span 栈"自然形成，本模块不手工传 parent。

三条必须记住的约束：

1. **不用 `asyncio.create_task` 把节点执行扔到后台**：上下文不会随之传播，子 span 会挂到
   错误的父节点上。`panel` 的三路并行是既有的、在 `panel` span 内部发起的调用，不受此限。
2. **`record_exception` 与 `raise` 必须成对**：记录异常不等于吞掉异常，守卫类节点
   fail-closed，异常必须继续上抛，否则会得到一张"看起来正常"的 trace。
3. **`node.ok` 用布尔而不是异常字符串**：后端要按 `node.ok = false` 聚合"各节点失败率"
   （14.3 链路级指标），异常文本进 `record_exception` 已经足够。

`OTEL_ENABLED=false` 时不注册 provider，`start_as_current_span` 退化为空操作 span，
性能开销可忽略，生产可按需关闭。导出失败不得影响问诊：批量导出器默认丢弃并记账，
本模块不额外抛错。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from langgraph.errors import GraphBubbleUp
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import NoOpTracerProvider, Span, Status, StatusCode, Tracer

from app.observability.attributes import (
    CONSULTATION_ID,
    EMPTY_VALUE,
    EVIDENCE_REF_IDS,
    EVIDENCE_REF_IDS_COUNT,
    EVIDENCE_SOURCE_TYPES,
    GATE_DECISION,
    GATE_RULES_HIT,
    INTERVIEW_QUESTION_IDS,
    INTERVIEW_SLOTS_HIT,
    LLM_DEGRADED,
    LLM_LATENCY_MS,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TOKENS,
    NODE_OK,
    NODE_ROUND,
    PROCESS_ID,
    bounded_ids,
    csv,
)

# 根 span 与 generation span 的固定名称（14.4 明文要求）。
ROOT_SPAN_NAME = "consultation"
GENERATION_SPAN_NAME = "generation"

_DEFAULT_SERVICE_NAME = "huanong-agent"

# 模块自持的 provider 引用，而非只依赖 OTel 全局单例。原因是可测性：
# 全局 provider 一生只能设置一次，多个用例各自注入内存导出器会互相冲突；自持引用
# 让每个用例都能独立配置与重置。未配置时回退到全局 provider（默认是空操作）。
_tracer_provider: TracerProvider | NoOpTracerProvider | None = None
_global_provider_set = False


def _settings() -> Any:
    """读配置。延迟导入避免 `app.core.config` 与可观测性在导入期形成环。"""
    from app.core.config import get_settings

    return get_settings()


def _tracer() -> Tracer:
    provider = _tracer_provider if _tracer_provider is not None else trace.get_tracer_provider()
    return provider.get_tracer(_DEFAULT_SERVICE_NAME)


def configure_tracing(
    settings: Any = None,
    *,
    exporter: SpanExporter | None = None,
    service_name: str | None = None,
) -> TracerProvider | None:
    """导出器三档：otlp（生产，指向自建 collector）、console（本地）、none（CI）。

    `exporter` 供测试注入内存导出器；生产只传 `settings`。`OTEL_ENABLED=false` 或
    `OTEL_EXPORTER=none` 时**不注册 provider**（置为空操作 provider），开销为零。

    用批量导出器（`BatchSpanProcessor`）而非简单导出器是硬要求：简单导出器在每次 span
    结束时同步阻塞，会把"农闲期也能用的服务"变成"collector 抖动即问诊变慢"。
    """
    global _tracer_provider, _global_provider_set

    settings = settings if settings is not None else _settings()
    enabled = getattr(settings, "OTEL_ENABLED", False)
    kind = getattr(settings, "OTEL_EXPORTER", "none")

    if exporter is None:
        if not enabled or kind == "none":
            # 空操作 provider 而不是"不设置"：后者会回退到全局 provider，可能已被别的
            # 用例设置过，导致"关闭追踪"仍产出 span。
            _tracer_provider = NoOpTracerProvider()
            return None
        exporter = _build_exporter(settings, kind)

    name = service_name or getattr(settings, "OTEL_SERVICE_NAME", None) or _DEFAULT_SERVICE_NAME
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _tracer_provider = provider
    # 全局 provider 只设一次：进程内多服务实例共存时以首个为准，避免重复设置抛错。
    if not _global_provider_set:
        trace.set_tracer_provider(provider)
        _global_provider_set = True
    return provider


def _build_exporter(settings: Any, kind: str) -> SpanExporter:
    """按档位构造导出器。otlp 的构造不建连接，endpoint 不可达只在导出时失败并被丢弃。"""
    if kind == "otlp":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=getattr(settings, "OTEL_ENDPOINT", "") or None)
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    return ConsoleSpanExporter()


def reset_tracing() -> None:
    """清空 provider 引用。供测试在用例之间隔离；生产不需要调用。"""
    global _tracer_provider
    _tracer_provider = None


def force_flush(timeout_millis: int = 5000) -> None:
    """把批量导出器的缓冲刷出。批量导出是异步的，测试断言前必须调用。"""
    provider = _tracer_provider
    if isinstance(provider, TracerProvider):
        provider.force_flush(timeout_millis)


def current_trace_context() -> dict[str, str] | None:
    """当前 span 的 `{trace_id, span_id}`；不在任何有效 span 内时返回 `None`。

    供日志模块（T6-08）把 trace 与日志关联。图内执行返回非空，图外返回 `None`。
    """
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


def _round_of(state: Mapping[str, Any]) -> int:
    """轮次。优先取预算里的问诊轮次，缺失时回退 `round` 键，都没有给 0。

    代码现状是预算对象承担轮次记账（`ConsultBudget.interview_rounds`，`round` 已无节点
    写入），因此先读预算；`process_id` 在 `intake` 之后、`triage` 之前为 `None`，属性值
    用 `"unknown"` 兜底而不是 `str(None)`，让"确实未知"与"字符串 None"可区分。
    """
    budget = state.get("budget")
    if isinstance(budget, Mapping):
        rounds = budget.get("interview_rounds")
        if isinstance(rounds, int):
            return rounds
    value = state.get("round")
    return value if isinstance(value, int) else 0


def _process_of(state: Mapping[str, Any]) -> str:
    process_id = state.get("process_id")
    if process_id is None:
        return "unknown"
    return str(getattr(process_id, "value", process_id))


def _set_node_attributes(span: Span, state: Mapping[str, Any]) -> None:
    session_id = state.get("session_id")
    span.set_attribute(CONSULTATION_ID, str(session_id) if session_id else EMPTY_VALUE)
    span.set_attribute(PROCESS_ID, _process_of(state))
    span.set_attribute(NODE_ROUND, _round_of(state))


def _set_outcome_attributes(span: Span, updates: Mapping[str, Any]) -> None:
    """按节点返回的增量写问诊 / 证据 / 闸门三组属性。

    放在 `traced_node` 里统一做，避免七个工厂各写一遍：属性来源就是节点的返回值，
    哪一组出现由该节点产出的键决定（`interview` 产出 `pending_questions`/`slots`，
    `verify` 产出 `evidence`，闸门节点产出 `gate_result`）。
    """
    questions = updates.get("pending_questions")
    if isinstance(questions, Sequence):
        span.set_attribute(
            INTERVIEW_QUESTION_IDS,
            csv([str(q.get("slot_key") or q.get("slotKey") or "") for q in questions if isinstance(q, Mapping)]),
        )
    slots = updates.get("slots")
    if isinstance(slots, Mapping):
        hit = [str(key) for key, slot in slots.items() if isinstance(slot, Mapping) and slot.get("status") == "filled"]
        span.set_attribute(INTERVIEW_SLOTS_HIT, csv(hit))

    evidence = updates.get("evidence")
    if isinstance(evidence, Sequence):
        rows = [row for row in evidence if isinstance(row, Mapping)]
        refs, total = bounded_ids([str(row.get("knowledge_id") or "") for row in rows])
        span.set_attribute(EVIDENCE_REF_IDS, refs)
        span.set_attribute(EVIDENCE_REF_IDS_COUNT, total)
        span.set_attribute(EVIDENCE_SOURCE_TYPES, csv([str(row.get("region_scope") or "") for row in rows]))

    gate = updates.get("gate_result")
    if isinstance(gate, Mapping):
        span.set_attribute(GATE_DECISION, str(gate.get("decision") or ""))
        violations = gate.get("violations")
        rules = [
            str(v.get("rule_id") or "")
            for v in (violations if isinstance(violations, Sequence) else [])
            if isinstance(v, Mapping)
        ]
        span.set_attribute(GATE_RULES_HIT, csv(rules))


def traced_node(name: str, fn: Any, *, span_name: str | None = None) -> Callable[..., Any]:
    """把一个节点函数包成带 span 的节点。

    包裹而非"节点内各写一行"：七个工厂与主图十余个节点若各自 `start_as_current_span`，
    字段口径必然分叉。包装后「每个智能体 = 一个 span」成为结构性质，不依赖执行者自觉。

    同步与异步节点都吃（主图 `confirm_object`/`subgraph_router`/`followup` 是同步函数）。
    包裹体保持与来源一致的调用形态，LangGraph 才能照常 `await` 异步节点、直接调用同步节点。

    `name` 同时作为 `__name__`（工厂依赖它区分流程，`test_factories` 会断言）；`span_name`
    用于 span 名，缺省与 `name` 相同。子图节点传入图上的注册名（如 `"observe"`），使
    "子 span 名为节点名"成立，而 `__name__` 仍是 `observe_rice`。

    异常处理的一条例外：LangGraph 的 `GraphBubbleUp`（含 `interrupt` 抛出的
    `GraphInterrupt`）是控制流信号而不是失败，必须原样上抛且**不**记 `node.ok=False`
    ——否则中断等待农户回答的节点会被统计成"失败节点"。
    """
    label = span_name or name

    # `record_exception=False, set_status_on_exception=False`：把"记异常/置失败状态"完全交给
    # 下面的显式分支。默认 True 会让**任何**穿过 `with` 的异常都被记成 ERROR，包括 LangGraph
    # 的中断控制流，从而把"等待农户回答"误报成"节点失败"。
    if inspect.iscoroutinefunction(fn):

        async def _wrapped_async(state: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
            with _tracer().start_as_current_span(label, record_exception=False, set_status_on_exception=False) as span:
                _set_node_attributes(span, state)
                try:
                    result = await fn(state, *args, **kwargs)
                except GraphBubbleUp:
                    raise
                except Exception as exc:
                    _mark_failure(span, exc)
                    raise
                _mark_success(span, result)
                return result

        _wrapped_async.__name__ = name
        return _wrapped_async

    def _wrapped_sync(state: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
        with _tracer().start_as_current_span(label, record_exception=False, set_status_on_exception=False) as span:
            _set_node_attributes(span, state)
            try:
                result = fn(state, *args, **kwargs)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                _mark_failure(span, exc)
                raise
            _mark_success(span, result)
            return result

    _wrapped_sync.__name__ = name
    return _wrapped_sync


def _mark_success(span: Span, result: Any) -> None:
    span.set_attribute(NODE_OK, True)
    if isinstance(result, Mapping):
        _set_outcome_attributes(span, result)


def _mark_failure(span: Span, exc: BaseException) -> None:
    """失败：属性记 `node.ok=False` 并置 span 状态为 `ERROR`，异常由调用方继续上抛。

    显式 `set_status(StatusCode.ERROR)`：`record_exception` 只把异常记进事件列表，
    不改变 span 状态；而后端按 `status = ERROR` 聚合失败率（14.3 链路级指标"各节点
    失败率"），不设状态就查不到。二者必须同时写。
    """
    span.set_attribute(NODE_OK, False)
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


@contextmanager
def consultation_span(session_id: str | None, process_id: object = None) -> Iterator[Span]:
    """一次问诊的根 span（14.4：一次问诊 = 一个 trace）。

    在问诊入口（`AIService` 调图处）打开，把 `session_id`（即检查点 `thread_id`）写进
    `consultation.id`，使 trace 可按会话检索。子节点与 generation 因处于当前 span 栈内，
    自动成为它的后代，无需手工传 parent。
    """
    with _tracer().start_as_current_span(ROOT_SPAN_NAME) as span:
        span.set_attribute(CONSULTATION_ID, str(session_id) if session_id else EMPTY_VALUE)
        span.set_attribute(PROCESS_ID, str(getattr(process_id, "value", process_id) or "unknown"))
        yield span


@contextmanager
def generation_span(task_type: str) -> Iterator[Span]:
    """一次模型调用的 generation span。属性在得到响应后用 `record_generation` 写入。

    `generation` 是固定名（14.4），`llm.task_type` 便于后端按任务类型分组成本。
    本模块不导入 `app.llm`（否则可观测性依赖模型层，且会与网关反向依赖形成环），
    因此响应字段以显式参数传入。
    """
    with _tracer().start_as_current_span(GENERATION_SPAN_NAME) as span:
        span.set_attribute("llm.task_type", task_type)
        yield span


def record_generation(
    span: Span,
    *,
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    degraded: bool,
) -> None:
    """写入 generation span 的五组字段。`tokens` 是输入输出的合计。"""
    span.set_attribute(LLM_MODEL, model)
    span.set_attribute(LLM_PROVIDER, provider)
    span.set_attribute(LLM_TOKENS, int(prompt_tokens) + int(completion_tokens))
    span.set_attribute(LLM_LATENCY_MS, int(latency_ms))
    span.set_attribute(LLM_DEGRADED, bool(degraded))


def spans_of(exporter: SpanExporter) -> list[ReadableSpan]:
    """取内存导出器里已导出的 span。导出器不是内存实现时返回空列表。"""
    getter = getattr(exporter, "get_finished_spans", None)
    return list(getter()) if callable(getter) else []


__all__ = [
    "GENERATION_SPAN_NAME",
    "ROOT_SPAN_NAME",
    "configure_tracing",
    "consultation_span",
    "current_trace_context",
    "force_flush",
    "generation_span",
    "record_generation",
    "reset_tracing",
    "spans_of",
    "traced_node",
]
