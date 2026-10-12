"""T6-04：主图路径断言（受理即分流、对象确认回环、handoff 出口）。

主图这一项的职责是**装配与接线**，不是智能体内部行为。因此用确定性叶子替身替换会调模型或
查库的节点，保留 `main.py` 的真实组装代码，让断言只盯「同一份输入走哪条路」。

本阶段文档引用的旧链路（`intake` 之后先 `collect_symptoms` 再 `diagnose`，路由发生在
`diagnose` 内部）在当前代码中不存在。新结构里「受理即分流」的落点是：`intake` 之后**第一条
边即条件边**，第二条 trace 元素只能是 `triage` 或 `subgraph_router`——不存在「先问症状再定
流程」的中间环节（对应技术方案 2.2 缺口一、9.2 第 11 条）。

三条断言分别覆盖：

1. **受理即分流**：`traced[0] == "intake"`，且 `triage` 早于任何 `subgraph_*`。
2. **对象确认回环**：triage 判对象未确认 → `confirm_object`；答复与档案冲突时经
   `route_after_confirm_object` 回分诊重判，再分流进子图（`confirm_object` 不得就地改流程）。
3. **handoff 出口**：红线未装载时 fail-closed，`intake` 之后直达 `handoff`，不进任何子图。

与 `test_main_graph.py`（T2-25）互补：那里断言闸门三态分支与未收敛出口，这里专测
「受理即分流」与「确认回环」两条路径形态。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.graph import gate_graph
from app.agent.graph import main as main_graph
from app.agent.state import ConsultStage, ConsultationState, ProcessId
from tests.graph.conftest import first_index

# 旧线性链的节点名（改造前形态）：出现即说明「受理即分流」被改回「先问再分流」。
_LEGACY_NODE_NAMES = ("collect_symptoms", "collect_management", "fetch_environment")


# ---- 叶子替身 ----


def _triage_stub(*, object_confirmed: bool, calls: list[int] | None = None) -> Callable[..., Awaitable[dict]]:
    """分诊替身：可记录调用次数，供「第二次分诊才确认对象」的回环用例使用。"""

    async def _run(state: ConsultationState) -> dict:
        if calls is not None:
            calls.append(1)
        return {
            "triage": {
                "process_id": "rice",
                "object_confirmed": object_confirmed,
                "object_label": "水稻·3亩",
                "slot_gaps": [],
                "profile_ready": False,
                "handoff_to_report": False,
            },
            "process_id": ProcessId.RICE,
            "stage": ConsultStage.TRIAGE,
        }

    return _run


def _triage_stub_confirm_on_second_call(calls: list[int]) -> Callable[..., Awaitable[dict]]:
    """第一次分诊说「对象未确认」，第二次说「已确认」——模拟农户答复后重判。"""

    async def _run(state: ConsultationState) -> dict:
        calls.append(1)
        confirmed = len(calls) > 1
        return {
            "triage": {
                "process_id": "rice",
                "object_confirmed": confirmed,
                "object_label": "水稻·3亩",
                "slot_gaps": [],
                "profile_ready": False,
                "handoff_to_report": False,
            },
            "process_id": ProcessId.RICE,
            "stage": ConsultStage.TRIAGE,
        }

    return _run


async def _handoff_stub(state: ConsultationState) -> dict:
    return {"stage": ConsultStage.HANDOFF, "reply": "已为您转接人工"}


def _prescribe_subgraph():
    """流程子图替身：单节点把 `stage` 置为 PRESCRIBE，让出口路由导向闸门。"""

    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.PRESCRIBE}

    graph = StateGraph(ConsultationState)
    graph.add_node("stub", _node)
    graph.add_edge(START, "stub")
    graph.add_edge("stub", END)
    return graph.compile()


def _gate_subgraph(decision: str = "allow"):
    """闸门子图替身：直接写 `gate_result`（真实引擎由 T2-45 / T6-04 `test_gate_branch.py` 覆盖）。"""

    async def _node(state: ConsultationState) -> dict:
        return {
            "gate_result": {"process_redlines_loaded": True, "decision": decision},
            "stage": ConsultStage.GATE if decision == "allow" else ConsultStage.BLOCKED,
        }

    graph = StateGraph(ConsultationState)
    graph.add_node("gate_check", _node)
    graph.add_edge(START, "gate_check")
    graph.add_edge("gate_check", END)
    return graph.compile()


async def _in_memory_checkpointer() -> InMemorySaver:
    return InMemorySaver()


def _patch_main(monkeypatch: pytest.MonkeyPatch, *, triage: Callable[..., Awaitable[dict]]) -> None:
    """装配替身：只替换叶子，保留 `main.py` 的真实接线（含真实的 `confirm_object`）。"""
    monkeypatch.setattr(main_graph, "get_checkpointer", _in_memory_checkpointer)
    monkeypatch.setattr(main_graph, "run_triage", triage)
    monkeypatch.setattr(main_graph, "build_report_guard_node", lambda *a, **k: _noop_guard())
    monkeypatch.setattr(main_graph, "build_handoff_node", lambda *a, **k: _handoff_stub)
    monkeypatch.setattr(main_graph, "SUBGRAPH_BUILDERS", {p: _prescribe_subgraph for p in ProcessId})
    monkeypatch.setattr(gate_graph, "build_gate_subgraph", lambda *a, **k: _gate_subgraph())


def _noop_guard() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"report_decision": {"triggered": False, "stop_consultation": False}}

    return _node


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-main-route",
        "user_id": "user-main-route",
        "process_hint": None,
        "redlines_loaded": True,
        "required_slots": [],
        "config_version": 1,
        "region_code": None,
        "slots": {},
        "prescription_history": [],
        "messages": [{"role": "user", "content": "我家水稻叶子发黄"}],
        "trace_id": "trace-main-route",
    }
    base.update(overrides)
    return base


async def _trace(graph: Any, state: dict, thread_id: str) -> list[str]:
    """跑图并收集顶层节点名序列（子图按挂载名计，不展开内部节点）。"""
    trace: list[str] = []
    config = {"configurable": {"thread_id": thread_id}}
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        trace.extend(chunk.keys())
    return trace


# ---- 1. 受理即分流 ----


async def test_intake_routes_before_any_subgraph(monkeypatch) -> None:
    """`traced[0] == "intake"`，且第二条即分诊——受理后第一件事是判流程，不是问问题。"""
    _patch_main(monkeypatch, triage=_triage_stub(object_confirmed=True))
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-main-intake-first")

    assert trace[0] == "intake"
    assert trace[1] == "triage"
    # 分诊早于任何流程子图（受理即分流的语义核心）。
    assert first_index(trace, "triage") < first_index(trace, "subgraph_router")


# ---- 2. 对象确认回环 ----


async def test_object_confirm_conflict_loops_back_to_triage(monkeypatch) -> None:
    """答复与档案冲突时回分诊重判，再分流进子图——`confirm_object` 不得就地改流程。"""
    calls: list[int] = []
    _patch_main(monkeypatch, triage=_triage_stub_confirm_on_second_call(calls))
    graph = await main_graph.build_graph()

    state = _state(
        confirm_object_answer="rice",
        profile_process_hint="tea",  # 与答复不一致 → 触发冲突回环
    )
    trace = await _trace(graph, state, "t-main-confirm-conflict")

    # 第一次分诊判「未确认」→ confirm_object；冲突 → 回 triage；第二次分诊确认后进子图。
    assert trace[:4] == ["intake", "triage", "confirm_object", "triage"]
    assert trace.count("triage") == 2, f"应回分诊一次，实际路径 {trace}"
    assert first_index(trace, "confirm_object") < trace.index("subgraph_router")
    assert "subgraph_router" in trace


async def test_object_confirm_resolved_enters_subgraph_without_loop(monkeypatch) -> None:
    """答复与档案一致时确认节点直接放行进子图，不回分诊。"""
    _patch_main(monkeypatch, triage=_triage_stub(object_confirmed=False))
    graph = await main_graph.build_graph()

    state = _state(confirm_object_answer="rice", profile_process_hint="rice")
    trace = await _trace(graph, state, "t-main-confirm-ok")

    assert trace[:3] == ["intake", "triage", "confirm_object"]
    assert trace.count("triage") == 1
    assert first_index(trace, "confirm_object") < first_index(trace, "subgraph_router")


# ---- 3. handoff 出口 ----


async def test_redlines_missing_exits_to_handoff(monkeypatch) -> None:
    """红线未装载时 fail-closed：`intake` 之后直达 `handoff`，不进任何子图。"""
    _patch_main(monkeypatch, triage=_triage_stub(object_confirmed=True))
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(redlines_loaded=False), "t-main-no-redlines")

    assert trace[0] == "intake"
    assert trace[1] == "handoff"
    assert not any(name.startswith("subgraph_") for name in trace)
