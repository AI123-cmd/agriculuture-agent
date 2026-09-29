"""T2-28：养猪流程子图的报告守卫结构特例。

养猪子图与其余四条流程的结构差异只有一处：报告守卫**同时**挂在 `observe` 之前（入口守卫
`report_guard_entry`）与 `observe` 之后（兜底守卫 `report_guard`）。原因是养猪的法定报告
信号体现在农户第一句描述里，而那一刻问诊尚未开始、`slots` 为空，只查槽位的信号表在最需要
触发的场景下会全部哑火（技术方案 7.4）。

本文件用**真实报告守卫节点**（规则实现、零模型调用）驱动断言，其余叶子节点用替身替换：

1. **入口守卫先于 observe**：未命中时 trace 首节点是 `report_guard_entry`，第二节点是
   `observe`，全流程不出现 `handoff`。
2. **命中即出子图**：农户首句命中报告信号时 trace 只有 `report_guard_entry`，不进入
   `observe` / `interview` / `prescribe`；经主图出口路由到达 `handoff`。
3. **命中后不回落问诊**：`route_after_subgraph_guard` 对已触发状态恒返回 `handoff`，
   与 `stage` 无关，因此复问也不会回到 `interview`。
4. **法定报告文案**：`reply` 同时含全部 `mandatory_actions` 条目与联系入口，且顺序与契约
   一致。
5. **特例只此一处**：`report_guard_entry` 仅出现在 `pig.py`。
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.agents.handoff import build_handoff_node
from app.agent.agents.report_guard import build_report_guard_node
from app.agent.contracts import ContactInfo
from app.agent.graph.routes import (
    ROUTE_AFTER_SUBGRAPH,
    route_after_subgraph,
    route_after_subgraph_guard,
)
from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.graph.subgraphs import pig as pig_module
from app.agent.state import ConsultStage, ConsultationState, ProcessId

_SUBGRAPH_DIR = Path(pig_module.__file__).parent

# 养猪报告信号的**真实**文本关键词（见 `agents/report_signals.py` 的 `_TEXT_SIGNAL_TABLE`）。
# 用真实关键词而非臆造症状名，才能证明信号表与守卫接线确实生效。
_TRIGGERING_MESSAGE = "我家猪昨天死了"

# 不含任何信号关键词的普通描述。
_NEUTRAL_MESSAGE = "我家猪有点不吃食"


class _FakeDirectory:
    """固定联系入口：让 `reply` 的联系方式段落有确定内容可断言。"""

    def for_region(self, region_code: str | None) -> list[ContactInfo]:
        return [
            ContactInfo(
                kind="vet_station",
                name="示例镇畜牧兽医站",
                phone="0591-0000000",
                region_code=region_code,
            )
        ]


# ---- 叶子替身 ----


def _observe() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.OBSERVE}

    return _node


def _interview() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.INTERVIEW, "slots": {}}

    return _node


def _verify() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.VERIFY}

    return _node


def _judge() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.JUDGE, "diagnosis": {"confidence_level": "likely", "out_of_scope": False}}

    return _node


def _prescribe() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.PRESCRIBE}

    return _node


def _patch_non_guard_leaves(monkeypatch) -> None:
    """只替换非守卫叶子；`build_report_guard_node` 保持真实实现。"""
    monkeypatch.setattr(pig_module, "make_observe_node", lambda config: _observe())
    monkeypatch.setattr(pig_module, "make_interview_node", lambda config: _interview())
    monkeypatch.setattr(pig_module, "make_verify_node", lambda config: _verify())
    monkeypatch.setattr(pig_module, "make_judge_node", lambda config: _judge())
    monkeypatch.setattr(pig_module, "make_prescribe_node", lambda config: _prescribe())


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-t2-28",
        "redlines_loaded": True,
        "required_slots": [],
        "slots": {},
        "config_version": 1,
        "process_id": ProcessId.PIG,
        "stage": ConsultStage.INTAKE,
        "messages": [],
    }
    base.update(overrides)
    return base


async def _trace(graph, state: dict) -> list[str]:
    names: list[str] = []
    async for chunk in graph.astream(state, stream_mode="updates"):
        names.extend(chunk.keys())
    return names


# ---- 1. 入口守卫先于 observe ----


async def test_guard_entry_runs_before_observe_when_not_triggered(monkeypatch) -> None:
    """未命中：trace 为入口守卫 → observe → 兜底守卫 → …，全程无 handoff。"""
    _patch_non_guard_leaves(monkeypatch)
    compiled = pig_module.build_pig_subgraph()

    trace = await _trace(compiled, _state(user_message=_NEUTRAL_MESSAGE))

    assert trace[0] == "report_guard_entry"
    assert trace[1] == "observe"
    assert trace.index("report_guard_entry") < trace.index("observe") < trace.index("report_guard")
    assert "handoff" not in trace
    assert trace == ["report_guard_entry", "observe", "report_guard", "interview", "verify", "judge", "prescribe"]


def test_pig_start_edge_targets_entry_guard(monkeypatch) -> None:
    """结构断言：`__start__` 指向 `report_guard_entry`，不直接指向 `observe`。"""
    compiled = SUBGRAPH_BUILDERS[ProcessId.PIG]()

    edges = {(edge.source, edge.target) for edge in compiled.get_graph().edges}
    assert ("__start__", "report_guard_entry") in edges
    assert ("__start__", "observe") not in edges
    assert ("observe", "report_guard") in edges


# ---- 2. 命中即出子图，并到达主图 handoff ----


async def test_triggered_at_entry_exits_subgraph_without_observe(monkeypatch) -> None:
    """农户首句命中报告信号：trace 只有入口守卫，不进入 observe / interview / prescribe。"""
    _patch_non_guard_leaves(monkeypatch)
    compiled = pig_module.build_pig_subgraph()

    trace = await _trace(compiled, _state(user_message=_TRIGGERING_MESSAGE))

    assert trace == ["report_guard_entry"]
    assert "observe" not in trace
    assert "interview" not in trace
    assert "prescribe" not in trace


async def test_triggered_guard_routes_to_handoff_on_main_graph(monkeypatch) -> None:
    """子图出口经 `route_after_subgraph` 落到 main 的 handoff 节点。

    用一个只含「子图 + handoff」的最小包装图验证接缝：子图只能走自身出口 `__end__`，
    由 `route_after_subgraph` 统一导向 `handoff`（LangGraph 子图边不指向父图节点）。
    """
    _patch_non_guard_leaves(monkeypatch)
    pig = pig_module.build_pig_subgraph()

    async def _handoff_stub(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.HANDOFF, "reply": "已为您转接人工"}

    async def _gate_stub(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.GATE}

    graph = StateGraph(ConsultationState)
    graph.add_node("subgraph_pig", pig)
    graph.add_node("handoff", _handoff_stub)
    graph.add_node("gate", _gate_stub)
    graph.add_edge(START, "subgraph_pig")
    graph.add_conditional_edges("subgraph_pig", route_after_subgraph, ROUTE_AFTER_SUBGRAPH)
    graph.add_edge("handoff", END)
    graph.add_edge("gate", END)
    wrapper = graph.compile()

    trace = await _trace(wrapper, _state(user_message=_TRIGGERING_MESSAGE))

    assert "handoff" in trace
    assert "gate" not in trace


# ---- 3. 命中后不回落问诊 ----


def test_triggered_guard_never_falls_back_to_interview() -> None:
    """`route_after_subgraph_guard` 对已触发状态恒返回 handoff，与 stage 无关。"""
    for stage in (ConsultStage.INTAKE, ConsultStage.OBSERVE, ConsultStage.INTERVIEW):
        state = _state(stage=stage, report_decision={"triggered": True, "stop_consultation": True})
        assert route_after_subgraph_guard(state) == "handoff"


# ---- 4. 法定报告文案含全部动作与联系入口 ----


async def test_legal_report_reply_contains_all_actions_and_contacts(monkeypatch) -> None:
    """`reply` 含全部 mandatory_actions（顺序一致）与联系入口。"""
    config = pig_module.get_config(ProcessId.PIG)
    guard = build_report_guard_node(config, contacts=_FakeDirectory())
    handoff = build_handoff_node(config, contacts=_FakeDirectory())

    graph = StateGraph(ConsultationState)
    graph.add_node("guard", guard)
    graph.add_node("handoff", handoff)
    graph.add_edge(START, "guard")
    graph.add_edge("guard", "handoff")
    graph.add_edge("handoff", END)
    compiled = graph.compile()

    result = await compiled.ainvoke(_state(user_message=_TRIGGERING_MESSAGE))

    package = result["handoff_package"]
    actions: list[str] = package["mandatory_actions"]
    assert actions  # 法定报告情形必须有强制动作
    reply: str = result["reply"]

    for action in actions:
        assert action in reply, f"reply 缺少强制动作：{action}"
    positions = [reply.index(action) for action in actions]
    assert positions == sorted(positions), "强制动作在 reply 中的顺序与契约不一致"

    assert package["contacts"]
    assert "联系方式：" in reply
    assert package["contacts"][0]["name"] in reply


# ---- 5. 特例只此一处 ----


def test_report_guard_entry_exists_only_in_pig_module() -> None:
    """`report_guard_entry` 是养猪专属，其他子图文件不得出现。"""
    hits = {
        path.name for path in _SUBGRAPH_DIR.glob("*.py") if "report_guard_entry" in path.read_text(encoding="utf-8")
    }
    assert hits == {"pig.py"}, hits
