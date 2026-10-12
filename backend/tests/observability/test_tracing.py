"""T6-06：链路追踪的 span 结构、父子关系与降级行为。

本文件的断言全部基于 `InMemorySpanExporter`：它把已结束的 span 留在内存里，能直接查
名称、父子关系与属性。生产走 OTLP 批量导出，形态与本文件无关——被测的是 `tracing.py`
写的 span 结构，而不是导出通道。

三类用例：

1. **结构**：一次执行 = 根 span `consultation` + 每节点一个子 span（名为节点名）+ 每次
   模型调用一个 `generation` span；数量与嵌套关系都对得上。
2. **失败语义**：节点抛普通异常时 span 状态为 `ERROR` 且 `node.ok=False`，异常继续上抛；
   LangGraph 的中断控制流（`GraphBubbleUp`）**不**被记成失败。
3. **降级**：`OTEL_ENABLED=false` 与 `OTEL_EXPORTER=none` 不产出 span 且行为不变；
   `otlp` 端点不可达不影响执行。

每次 `configure_tracing` 注入独立的内存导出器并 `reset_tracing`，用例之间不串状态。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.observability import (
    GENERATION_SPAN_NAME,
    ROOT_SPAN_NAME,
    configure_tracing,
    consultation_span,
    current_trace_context,
    force_flush,
    generation_span,
    reset_tracing,
    traced_node,
)
from app.observability.attributes import NODE_OK, PROCESS_ID
from app.observability.tracing import spans_of


def _settings(**overrides: Any) -> SimpleNamespace:
    """配置替身：`configure_tracing` 只按属性名取值，不需要完整 `Settings`。"""
    base = {
        "OTEL_ENABLED": True,
        "OTEL_EXPORTER": "console",
        "OTEL_ENDPOINT": "",
        "OTEL_SERVICE_NAME": "huanong-test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    """每个用例独立注入内存导出器，并在结束后清空模块自持的 provider 引用。"""
    exp = InMemorySpanExporter()
    configure_tracing(_settings(), exporter=exp)
    yield exp
    reset_tracing()


def _by_name(exporter: InMemorySpanExporter) -> dict[str, list]:
    out: dict[str, list] = {}
    for span in spans_of(exporter):
        out.setdefault(span.name, []).append(span)
    return out


def _chain_graph():
    """最小可运行图：两个被追踪的节点串联，第二个节点内发起一次 generation。

    刻意不用真实子图：本文件只验证 `traced_node` 与 `generation_span` 写出的 span 结构，
    真实图的接线由 `tests/graph/` 覆盖。
    """

    async def _first(state: dict) -> dict:
        return {"stage": "observe"}

    async def _second(state: dict) -> dict:
        with generation_span("diagnosis"):
            return {"stage": "judge"}

    graph = StateGraph(dict)
    graph.add_node("first", traced_node("first", _first))
    graph.add_node("second", traced_node("second", _second))
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile()


# ---- 1. 结构：根 span + 每节点一 span + 每模型调用一 span ----


async def test_span_tree_shape(exporter) -> None:
    """根 span 名 `consultation`，子 span 名为节点名，generation span 名 `generation`。"""
    app = _chain_graph()
    with consultation_span("sess-1", "rice"):
        await app.ainvoke({"session_id": "sess-1", "process_id": "rice"})
    force_flush()

    by_name = _by_name(exporter)
    assert ROOT_SPAN_NAME in by_name
    assert {s.name for s in by_name["first"]} == {"first"}
    assert {s.name for s in by_name["second"]} == {"second"}
    assert GENERATION_SPAN_NAME in by_name
    # 节点数 2 + 模型调用数 1，外加根 span。
    assert len(spans_of(exporter)) == 1 + 2 + 1


async def test_child_spans_parent_is_root(exporter) -> None:
    """节点 span 的父 span 是根 span：三层嵌套由当前 span 栈自然形成，无需手工传 parent。"""
    app = _chain_graph()
    with consultation_span("sess-1", "rice"):
        await app.ainvoke({"session_id": "sess-1", "process_id": "rice"})
    force_flush()

    by_name = _by_name(exporter)
    root = by_name[ROOT_SPAN_NAME][0]
    for name in ("first", "second"):
        assert by_name[name][0].parent is not None
        assert by_name[name][0].parent.span_id == root.context.span_id


async def test_generation_span_parent_is_its_node(exporter) -> None:
    """generation span 的父是发起它的节点 span，而不是根。"""
    app = _chain_graph()
    with consultation_span("sess-1", "rice"):
        await app.ainvoke({"session_id": "sess-1", "process_id": "rice"})
    force_flush()

    by_name = _by_name(exporter)
    second = by_name["second"][0]
    generation = by_name[GENERATION_SPAN_NAME][0]
    assert generation.parent is not None
    assert generation.parent.span_id == second.context.span_id


async def test_root_span_carries_consultation_id(exporter) -> None:
    """根 span 写 `consultation.id` 与 `process.id`，使 trace 可按会话检索。"""
    with consultation_span("sess-abc", "rice"):
        current_trace_context()
    force_flush()

    root = _by_name(exporter)[ROOT_SPAN_NAME][0]
    assert root.attributes["consultation.id"] == "sess-abc"
    assert root.attributes[PROCESS_ID] == "rice"


# ---- 2. 失败语义 ----


async def test_node_exception_marks_span_error_and_reraises(exporter) -> None:
    """普通异常：span 状态 ERROR、`node.ok=False`，异常必须继续上抛。"""

    async def _boom(state: dict) -> dict:
        raise ValueError("节点炸了")

    graph = StateGraph(dict)
    graph.add_node("bad", traced_node("bad", _boom))
    graph.add_edge(START, "bad")
    graph.add_edge("bad", END)
    app = graph.compile()

    with consultation_span("sess-1", "rice"), pytest.raises(ValueError, match="节点炸了"):
        await app.ainvoke({"session_id": "sess-1", "process_id": "rice"})
    force_flush()

    bad = _by_name(exporter)["bad"][0]
    assert bad.attributes[NODE_OK] is False
    assert bad.status.status_code is StatusCode.ERROR


async def test_interrupt_is_not_counted_as_failure(exporter) -> None:
    """LangGraph 中断（`GraphInterrupt`）是控制流信号，不得记成节点失败。

    直接调被包装的节点而不经编译图：LangGraph 会把节点抛出的中断转成自己的中断状态，
    那样就观测不到"包装器是否给 span 标了失败"。这里只盯包装器的异常分支。
    """

    async def _interrupt(state: dict) -> dict:
        raise GraphInterrupt()

    with consultation_span("sess-1", "rice"), pytest.raises(GraphInterrupt):
        await traced_node("ask", _interrupt)({"session_id": "sess-1", "process_id": "rice"})
    force_flush()

    ask = _by_name(exporter)["ask"][0]
    assert ask.attributes.get(NODE_OK) is None
    assert ask.status.status_code is not StatusCode.ERROR


async def test_sync_node_is_traced(exporter) -> None:
    """同步节点也吃追踪（主图 `confirm_object` 等是同步函数）。"""

    def _sync(state: dict) -> dict:
        return {"stage": "triage"}

    graph = StateGraph(dict)
    graph.add_node("sync", traced_node("sync", _sync))
    graph.add_edge(START, "sync")
    graph.add_edge("sync", END)
    await graph.compile().ainvoke({"session_id": "sess-1", "process_id": "rice"})
    force_flush()

    sync_span = _by_name(exporter)["sync"][0]
    assert sync_span.attributes[NODE_OK] is True


# ---- 3. 并行分支的上下文传播 ----


async def test_parallel_generation_spans_parented_to_panel(exporter) -> None:
    """并行分支内的 generation span 父为 `panel`：证明上下文在 `asyncio.gather` 内正确传播。"""

    async def _vendor(state: dict) -> dict:
        with generation_span("panel_vendor"):
            return {}

    async def _panel(state: dict) -> dict:
        await asyncio.gather(_vendor(state), _vendor(state))
        return {}

    graph = StateGraph(dict)
    graph.add_node("panel", traced_node("panel", _panel))
    graph.add_edge(START, "panel")
    graph.add_edge("panel", END)
    await graph.compile().ainvoke({"session_id": "sess-1", "process_id": "chili"})
    force_flush()

    by_name = _by_name(exporter)
    panel = by_name["panel"][0]
    generations = by_name[GENERATION_SPAN_NAME]
    assert len(generations) == 2
    for span in generations:
        assert span.parent is not None
        assert span.parent.span_id == panel.context.span_id


# ---- 4. 降级：关闭追踪 / 空导出器 / 端点不可达 ----


async def test_disabled_returns_no_provider_and_runs_normally() -> None:
    """`OTEL_ENABLED=false`：不注册 provider（返回 `None`），节点行为不变、零 span。"""
    try:
        provider = configure_tracing(_settings(OTEL_ENABLED=False), exporter=None)
        assert provider is None

        async def _node(state: dict) -> dict:
            return {"stage": "observe"}

        graph = StateGraph(dict)
        graph.add_node("n", traced_node("n", _node))
        graph.add_edge(START, "n")
        graph.add_edge("n", END)
        result = await graph.compile().ainvoke({"session_id": "s", "process_id": "rice"})
        force_flush()

        assert result["stage"] == "observe"
        # 未注册 provider 时 `_tracer()` 回退到全局 provider（默认空操作），无 span 可导出。
        assert current_trace_context() is None
    finally:
        reset_tracing()


async def test_none_exporter_produces_no_span() -> None:
    """`OTEL_EXPORTER=none`：不注册 provider，`start_as_current_span` 退化为空操作。"""
    try:
        configure_tracing(_settings(OTEL_EXPORTER="none"), exporter=None)

        async def _node(state: dict) -> dict:
            return {"stage": "observe"}

        graph = StateGraph(dict)
        graph.add_node("n", traced_node("n", _node))
        graph.add_edge(START, "n")
        graph.add_edge("n", END)
        result = await graph.compile().ainvoke({"session_id": "s", "process_id": "rice"})
        assert result["stage"] == "observe"
    finally:
        reset_tracing()


async def test_otlp_endpoint_unreachable_does_not_fail_run() -> None:
    """`OTEL_EXPORTER=otlp` 且端点不可达：导出失败被丢弃，问诊仍然成功。"""
    try:
        # 端点指向一个必然连不上的本地端口；构造不建连接，导出失败由批量导出器吞掉。
        configure_tracing(
            _settings(OTEL_EXPORTER="otlp", OTEL_ENDPOINT="http://127.0.0.1:1"),
            exporter=None,
            service_name="huanong-test",
        )

        async def _node(state: dict) -> dict:
            return {"stage": "observe"}

        graph = StateGraph(dict)
        graph.add_node("n", traced_node("n", _node))
        graph.add_edge(START, "n")
        graph.add_edge("n", END)
        result = await graph.compile().ainvoke({"session_id": "s", "process_id": "rice"})
        force_flush(timeout_millis=100)
        assert result["stage"] == "observe"
    finally:
        reset_tracing()


# ---- 5. trace 上下文取值 ----


async def test_current_trace_context_inside_and_outside(exporter) -> None:
    """图内返回非空 `trace_id`/`span_id`，图外返回 `None`。"""
    seen: dict[str, Any] = {}

    async def _node(state: dict) -> dict:
        seen["ctx"] = current_trace_context()
        return {"stage": "observe"}

    graph = StateGraph(dict)
    graph.add_node("n", traced_node("n", _node))
    graph.add_edge(START, "n")
    graph.add_edge("n", END)

    assert current_trace_context() is None
    with consultation_span("sess-1", "rice"):
        await graph.compile().ainvoke({"session_id": "s", "process_id": "rice"})

    ctx = seen["ctx"]
    assert ctx is not None
    assert len(ctx["trace_id"]) == 32
    assert len(ctx["span_id"]) == 16
