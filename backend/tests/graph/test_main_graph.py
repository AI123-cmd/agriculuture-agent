"""T2-25：主图组装的路径与结构断言。

主图这一项的职责是**装配与接线**（受理即分流、子图挂载、闸门位置、回环出口），不是
智能体内部行为——后者由各自的 T2-14/22/26/45 覆盖。因此本文件用**确定性的节点替身**
替换会调模型或查库的叶子节点，保留 `main.py` 的真实组装代码，让断言盯住"同一份输入
走哪条路"：

1. **受理即分流**：`trace[0] == "intake"`，`trace[1] in {"triage", "subgraph_router"}`；
   入口带 `process_hint` 时直达 `subgraph_router`，不带时先 `triage`。
2. **报告信号早退**：分诊 `handoff_to_report=True` 时路径为 `intake → triage → report_guard
   → handoff`，且不进入任何 `subgraph_*`。
3. **养猪子图结构**：真子图里入口守卫 `report_guard_entry` 出现在 `observe` 之前。
4. **未收敛出口**：`interview_rounds` 用尽且槽位仍缺时，问诊只跑一次就出子图，不无限回环。
5. **闸门分支**：`degrade` 时路径为 `gate → handoff`，不经过 `followup`。
6. **编译与旧链路**：`build_graph()` 返回编译图对象；`main.py` 不再引用旧的线性链节点名。

叶子替身覆盖 `run_triage`、主图入口 `report_guard`、`handoff`、`gate` 子图与七条流程
子图；七个 `*_MAP` 条件边与 `route_*` 全是真实实现，接线错了会当场失败。
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.graph import gate_graph
from app.agent.graph import main as main_graph
from app.agent.graph.subgraphs import pig as pig_subgraph
from app.agent.graph.subgraphs import rice as rice_subgraph
from app.agent.state import ConsultStage, ConsultationState, ProcessId

# 旧线性链的节点名（T2-25 要求整体删除）：出现即说明旧链路被复用。
_LEGACY_NODE_NAMES = ("collect_symptoms", "collect_management", "fetch_environment")


# ---- 叶子替身 ----


def _triage_stub(**overrides: Any) -> Callable[[ConsultationState], Awaitable[dict]]:
    """分诊替身：返回固定 `TriageResult` 等价 dict，绕开模型与槽位计算。"""
    payload: dict[str, Any] = {
        "process_id": "rice",
        "object_confirmed": True,
        "object_label": "水稻·3亩",
        "slot_gaps": [],
        "profile_ready": False,
        "handoff_to_report": False,
    }
    payload.update(overrides)

    async def _run(state: ConsultationState) -> dict:
        return {
            "triage": payload,
            "process_id": ProcessId(payload["process_id"]),
            "stage": ConsultStage.TRIAGE,
        }

    return _run


def _entry_guard_stub(triggered: bool) -> Callable[[ConsultationState], Awaitable[dict]]:
    """主图入口守卫替身：直接给出报告判定，跳过信号匹配。"""

    async def _node(state: ConsultationState) -> dict:
        return {"report_decision": {"triggered": triggered, "stop_consultation": triggered}}

    return _node


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


def _gate_subgraph(decision: str):
    """闸门子图替身：直接写 `gate_result`，隔离掉引擎与审计（另由 T2-45/T2-46 覆盖）。"""

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


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    triage: Callable[[ConsultationState], Awaitable[dict]] | None = None,
    guard_triggered: bool = False,
    gate_decision: str = "allow",
) -> None:
    """装配替身：只替换叶子，保留 `main.py` 的真实接线。"""
    monkeypatch.setattr(main_graph, "get_checkpointer", _in_memory_checkpointer)
    monkeypatch.setattr(main_graph, "run_triage", triage or _triage_stub())
    monkeypatch.setattr(main_graph, "build_report_guard_node", lambda *a, **k: _entry_guard_stub(guard_triggered))
    monkeypatch.setattr(main_graph, "build_handoff_node", lambda *a, **k: _handoff_stub)
    monkeypatch.setattr(main_graph, "SUBGRAPH_BUILDERS", {p: _prescribe_subgraph for p in ProcessId})
    monkeypatch.setattr(gate_graph, "build_gate_subgraph", lambda *a, **k: _gate_subgraph(gate_decision))


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-0001",
        "user_id": "user-0001",
        "process_hint": None,
        "redlines_loaded": True,
        "required_slots": [],
        "config_version": 1,
        "region_code": None,
        "slots": {},
        "prescription_history": [],
        "messages": [{"role": "user", "content": "我家水稻叶子发黄"}],
        "trace_id": "trace-0001",
    }
    base.update(overrides)
    return base


async def _trace(graph, state: dict, thread_id: str) -> list[str]:
    """跑一遍并收集顶层节点名序列（子图按挂载名计，不展开内部节点）。"""
    trace: list[str] = []
    config = {"configurable": {"thread_id": thread_id}}
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        trace.extend(chunk.keys())
    return trace


# ---- 受理即分流 ----


async def test_route_at_intake_when_process_not_locked(monkeypatch) -> None:
    """不带 `process_hint` 时，第二条 trace 元素是 `triage`（先判流程再问）。"""
    _patch_main(monkeypatch)
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-route-triage")

    assert trace[0] == "intake"
    assert trace[1] == "triage"


async def test_route_at_intake_when_process_hint_locked(monkeypatch) -> None:
    """带 `process_hint` 时入口直达 `subgraph_router`，不再经过 `triage`。"""
    _patch_main(monkeypatch)
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(process_hint="rice"), "t-route-locked")

    assert trace[0] == "intake"
    assert trace[1] == "subgraph_router"
    assert "triage" not in trace


async def test_handoff_when_redlines_missing(monkeypatch) -> None:
    """红线未装载时受理即分流到转人工（fail-closed），不进任何子图。"""
    _patch_main(monkeypatch)
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(redlines_loaded=False), "t-no-redlines")

    assert trace[0] == "intake"
    assert trace[1] == "handoff"
    assert not any(name.startswith("subgraph_") for name in trace)


# ---- 报告信号早退 ----


async def test_handoff_to_report_path_skips_subgraphs(monkeypatch) -> None:
    """`handoff_to_report=True` 时路径为 intake → triage → report_guard → handoff。"""
    _patch_main(monkeypatch, triage=_triage_stub(process_id="pig", handoff_to_report=True), guard_triggered=True)
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-report-signal")

    assert trace[:4] == ["intake", "triage", "report_guard", "handoff"]
    assert not any(name.startswith("subgraph_") for name in trace)


async def test_report_guard_passes_without_trigger(monkeypatch) -> None:
    """未命中报告信号时入口守卫放行，进入流程子图而非转人工。"""
    _patch_main(monkeypatch, triage=_triage_stub(process_id="pig", handoff_to_report=True), guard_triggered=False)
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-report-pass")

    assert "report_guard" in trace
    assert any(name.startswith("subgraph_") for name in trace)
    assert "handoff" not in trace


# ---- 闸门分支 ----


async def test_gate_allow_goes_followup(monkeypatch) -> None:
    """`allow` 时 gate → followup → END。"""
    _patch_main(monkeypatch, gate_decision="allow")
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-gate-allow")

    assert trace[-2:] == ["gate", "followup"]


async def test_gate_degrade_goes_handoff(monkeypatch) -> None:
    """`degrade` 时 gate → handoff，不经过 followup。"""
    _patch_main(monkeypatch, gate_decision="degrade")
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-gate-degrade")

    assert trace[-2:] == ["gate", "handoff"]
    assert "followup" not in trace


async def test_gate_block_goes_handoff(monkeypatch) -> None:
    """`block` 时同样出到 handoff（stage 由闸门子图自行置 BLOCKED）。"""
    _patch_main(monkeypatch, gate_decision="block")
    graph = await main_graph.build_graph()

    trace = await _trace(graph, _state(), "t-gate-block")

    assert trace[-2:] == ["gate", "handoff"]


# ---- 编译产物与接线 ----


async def test_build_graph_compiles_and_has_expected_edges(monkeypatch) -> None:
    """编译成功，且入口、分诊条件边、闸门出口的接线与 T2-25 一致。"""
    _patch_main(monkeypatch)
    graph = await main_graph.build_graph()

    nodes = set(graph.get_graph().nodes)
    assert "intake" in nodes
    assert "subgraph_router" in nodes
    assert "gate" in nodes

    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("__start__", "intake") in edges
    assert ("followup", "__end__") in edges
    assert ("handoff", "__end__") in edges
    # 受理后第一条是条件边：intake 的三个去向都在（triage / subgraph_router / handoff）
    assert {"triage", "subgraph_router", "handoff"} <= {target for source, target in edges if source == "intake"}


def test_main_module_does_not_reference_legacy_nodes() -> None:
    """旧线性链节点名不得出现在主图源码里（T2-25 删除项）。"""
    source = inspect.getsource(main_graph)
    for legacy in _LEGACY_NODE_NAMES:
        assert legacy not in source


# ---- 养猪子图：入口守卫先于 observe ----


def test_pig_subgraph_guard_runs_before_observe() -> None:
    """真子图结构：START → report_guard_entry（守卫）→ observe → report_guard（兜底）。"""
    compiled = pig_subgraph.build_pig_subgraph()

    nodes = set(compiled.get_graph().nodes)
    assert {"report_guard_entry", "observe", "report_guard"} <= nodes

    edges = {(edge.source, edge.target) for edge in compiled.get_graph().edges}
    assert ("__start__", "report_guard_entry") in edges
    assert ("__start__", "observe") not in edges
    assert ("observe", "report_guard") in edges


# ---- 未收敛出口：问诊不无限回环 ----


async def test_interview_exhausted_exits_subgraph_instead_of_looping(monkeypatch) -> None:
    """`interview_rounds` 用尽且槽位仍缺时，问诊只跑一次即以 handoff 出子图。"""
    calls: list[int] = []

    def _observe(config):
        async def _node(state):
            return {"stage": ConsultStage.OBSERVE}

        return _node

    def _guard(config):
        async def _node(state):
            return {"report_decision": {"triggered": False, "stop_consultation": False}}

        return _node

    def _interview(config):
        async def _node(state):
            calls.append(1)
            return {}  # 不改 state：槽位仍 missing、轮次不增，模拟"未收敛"

        return _node

    monkeypatch.setattr(rice_subgraph, "make_observe_node", _observe)
    monkeypatch.setattr(rice_subgraph, "build_report_guard_node", _guard)
    monkeypatch.setattr(rice_subgraph, "make_interview_node", _interview)

    compiled = rice_subgraph.build_rice_subgraph()
    state = _state(
        process_id="rice",
        stage=ConsultStage.OBSERVE,
        required_slots=["symptom"],
        slots={"symptom": {"key": "symptom", "value": None, "status": "missing", "source": "inferred"}},
        budget={"interview_rounds": 4, "max_interview_rounds": 4},
    )

    result = await compiled.ainvoke(state)

    assert len(calls) == 1  # 未回环
    assert result["stage"] != ConsultStage.PRESCRIBE  # 未走到开方即出子图


async def test_interview_loops_once_when_budget_remains(monkeypatch) -> None:
    """预算仍有剩时问诊应回环一次，作为上一条用例的反向对照。"""
    calls: list[int] = []

    def _observe(config):
        async def _node(state):
            return {"stage": ConsultStage.OBSERVE}

        return _node

    def _guard(config):
        async def _node(state):
            return {"report_decision": {"triggered": False, "stop_consultation": False}}

        return _node

    def _interview(config):
        async def _node(state):
            calls.append(1)
            if len(calls) == 1:
                # 第一轮仍未补齐：触发未收敛回环
                return {
                    "stage": ConsultStage.INTERVIEW,
                    "slots": {"symptom": {"key": "symptom", "value": None, "status": "missing", "source": "inferred"}},
                }
            # 第二轮补上槽位，使路由进入 verify 分支，避免真的无限循环
            return {
                "stage": ConsultStage.INTERVIEW,
                "slots": {
                    "symptom": {"key": "symptom", "value": "叶片发黄", "status": "filled", "source": "user_answer"}
                },
            }

        return _node

    def _verify(config):
        async def _node(state):
            return {"stage": ConsultStage.VERIFY}

        return _node

    def _judge(config):
        async def _node(state):
            return {
                "diagnosis": {"confidence_level": "likely", "out_of_scope": False},
                "stage": ConsultStage.JUDGE,
            }

        return _node

    def _prescribe(config):
        async def _node(state):
            return {"stage": ConsultStage.PRESCRIBE}

        return _node

    monkeypatch.setattr(rice_subgraph, "make_observe_node", _observe)
    monkeypatch.setattr(rice_subgraph, "build_report_guard_node", _guard)
    monkeypatch.setattr(rice_subgraph, "make_interview_node", _interview)
    monkeypatch.setattr(rice_subgraph, "make_verify_node", _verify)
    monkeypatch.setattr(rice_subgraph, "make_judge_node", _judge)
    monkeypatch.setattr(rice_subgraph, "make_prescribe_node", _prescribe)

    compiled = rice_subgraph.build_rice_subgraph()
    state = _state(
        process_id="rice",
        stage=ConsultStage.OBSERVE,
        required_slots=["symptom"],
        slots={"symptom": {"key": "symptom", "value": None, "status": "missing", "source": "inferred"}},
        budget={"interview_rounds": 0, "max_interview_rounds": 4},
    )

    result = await compiled.ainvoke(state)

    assert len(calls) == 2  # 第一次未收敛 → 回环 → 第二次补齐
    assert result["stage"] == ConsultStage.PRESCRIBE
