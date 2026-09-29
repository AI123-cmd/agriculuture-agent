"""T2-27：一期五条流程子图的结构与节点顺序断言。

一期落地水稻 / 茶 / 辣椒 / 杂草 / 养猪五条流程子图，注册进 `SUBGRAPH_BUILDERS`。本文件
用**确定性的叶子替身**替换会调模型或查库的五个通用节点，保留 `rice.py` / `tea.py` /
`chili.py` / `weed.py` / `pig.py` 的真实组装代码，让断言盯住「同一份输入走哪条路」：

1. **节点顺序**：`observe` 先于 `report_guard`，`report_guard` 先于 `interview`，
   `interview` 先于 `judge`，`judge` 先于 `prescribe`（技术方案 7.1）。
2. **守卫节点存在**：每条子图都注册名为 `report_guard` 的节点。
3. **会诊分支**：茶 / 辣椒 / 杂草在 `panel_needed=True` 时 trace 含 `panel`，且落在
   `interview` 与 `verify` 之间；水稻任意状态都不含 `panel`。
4. **注册表**：一期五条流程全部在 `SUBGRAPH_BUILDERS` 中（T2-29 再补油菜与通用两条）。
5. **行数上限**：五条子图的有效行数极差不超过 14（同构流程不应出现体量漂移）。

子图之间唯一的差异来自 `ProcessConfig`（技术方案 7.1「流程配置是数据而非代码」），因此
断言只区分「有没有 panel 节点」这一项配置差异，其余结构对五条流程一致。
"""

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.graph.subgraphs import chili as chili_module
from app.agent.graph.subgraphs import pig as pig_module
from app.agent.graph.subgraphs import rice as rice_module
from app.agent.graph.subgraphs import tea as tea_module
from app.agent.graph.subgraphs import weed as weed_module
from app.agent.state import ConsultStage, ConsultationState, ProcessId

# 一期五条流程；注册表的完整七条由 T2-29 断言。
_PHASE1_PROCESS_IDS = (
    ProcessId.RICE,
    ProcessId.TEA,
    ProcessId.CHILI,
    ProcessId.WEED,
    ProcessId.PIG,
)

# 会诊启用（panel_enabled=true）的三条流程，与 `routes.PANEL_ENABLED_PROCESSES` 对齐。
_PANEL_PROCESS_IDS = (ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED)

# 有效行数上限：五条同构子图之间不应出现体量漂移。
_MAX_LINE_SPREAD = 14


# ---- 叶子替身 ----


def _observe() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.OBSERVE}

    return _node


def _guard() -> Callable[[ConsultationState], Awaitable[dict]]:
    """兜底守卫替身：未触发，放行问诊。"""

    async def _node(state: ConsultationState) -> dict:
        return {"report_decision": {"triggered": False, "stop_consultation": False}}

    return _node


def _interview(panel_needed: bool) -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.INTERVIEW, "panel_needed": panel_needed, "slots": {}}

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


def _panel() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"panel_ran": True}

    return _node


def _patch_leaf_factories(monkeypatch, module, *, panel_needed: bool, with_panel: bool) -> None:
    """把子图模块里的叶子工厂换成替身。保留子图文件真实的接线代码。"""
    monkeypatch.setattr(module, "make_observe_node", lambda config: _observe())
    monkeypatch.setattr(module, "build_report_guard_node", lambda config: _guard())
    monkeypatch.setattr(module, "make_interview_node", lambda config: _interview(panel_needed))
    monkeypatch.setattr(module, "make_verify_node", lambda config: _verify())
    monkeypatch.setattr(module, "make_judge_node", lambda config: _judge())
    monkeypatch.setattr(module, "make_prescribe_node", lambda config: _prescribe())
    if with_panel:
        monkeypatch.setattr(module, "make_panel_node", lambda config: _panel())


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-t2-27",
        "redlines_loaded": True,
        "required_slots": [],
        "slots": {},
        "config_version": 1,
        "messages": [],
    }
    base.update(overrides)
    return base


async def _trace(graph, state: dict) -> list[str]:
    """跑一遍并收集顶层节点名序列（子图内节点按名字计）。"""
    names: list[str] = []
    async for chunk in graph.astream(state, stream_mode="updates"):
        names.extend(chunk.keys())
    return names


def _order_ok(trace: list[str]) -> bool:
    """observe < report_guard < interview < judge < prescribe 的子序列约束。"""
    expected = ["observe", "report_guard", "interview", "judge", "prescribe"]
    positions = [trace.index(name) for name in expected if name in trace]
    return positions == sorted(positions) and len(positions) == len(expected)


# ---- 1. 注册表含一期五条 ----


def test_registry_contains_all_phase1_flows() -> None:
    """一期五条流程全部注册进 `SUBGRAPH_BUILDERS`（完整七条见 T2-29）。"""
    registered = {process_id.value for process_id in SUBGRAPH_BUILDERS}
    assert {process_id.value for process_id in _PHASE1_PROCESS_IDS} <= registered


# ---- 2. 每条子图都注册 report_guard 节点 ----


def test_every_phase1_subgraph_registers_report_guard_node() -> None:
    for process_id in _PHASE1_PROCESS_IDS:
        compiled = SUBGRAPH_BUILDERS[process_id]()
        assert "report_guard" in set(compiled.get_graph().nodes), f"{process_id.value} 缺少 report_guard 节点"


# ---- 3. 节点顺序 ----


async def test_non_panel_flows_follow_expected_node_order(monkeypatch) -> None:
    """水稻：observe → report_guard → interview → verify → judge → prescribe。"""
    _patch_leaf_factories(monkeypatch, rice_module, panel_needed=False, with_panel=False)
    compiled = rice_module.build_rice_subgraph()

    trace = await _trace(compiled, _state(process_id=ProcessId.RICE))

    assert trace == ["observe", "report_guard", "interview", "verify", "judge", "prescribe"]
    assert _order_ok(trace)


async def test_panel_flows_follow_expected_node_order(monkeypatch) -> None:
    """茶 / 辣椒 / 杂草：interview 之后进 panel，panel 之后才 verify。"""
    modules = {
        ProcessId.TEA: tea_module,
        ProcessId.CHILI: chili_module,
        ProcessId.WEED: weed_module,
    }
    for process_id, module in modules.items():
        _patch_leaf_factories(monkeypatch, module, panel_needed=True, with_panel=True)
        builder = SUBGRAPH_BUILDERS[process_id]
        compiled = builder()

        trace = await _trace(compiled, _state(process_id=process_id))

        assert _order_ok(trace), f"{process_id.value} 节点顺序异常：{trace}"
        assert "panel" in trace, f"{process_id.value} 未在 panel_needed=True 时进入会诊"
        assert trace.index("interview") < trace.index("panel") < trace.index("verify")


async def test_rice_never_enters_panel(monkeypatch) -> None:
    """水稻即便状态携带 panel_needed=True 也不会出现 panel 节点（未注册）。"""
    _patch_leaf_factories(monkeypatch, rice_module, panel_needed=True, with_panel=False)
    compiled = rice_module.build_rice_subgraph()

    trace = await _trace(compiled, _state(process_id=ProcessId.RICE, panel_needed=True))

    assert "panel" not in trace


async def test_pig_follows_expected_node_order(monkeypatch) -> None:
    """养猪：入口守卫 + observe + 兜底守卫，其后与其余流程一致。"""
    monkeypatch.setattr(pig_module, "make_observe_node", lambda config: _observe())
    monkeypatch.setattr(pig_module, "make_interview_node", lambda config: _interview(False))
    monkeypatch.setattr(pig_module, "make_verify_node", lambda config: _verify())
    monkeypatch.setattr(pig_module, "make_judge_node", lambda config: _judge())
    monkeypatch.setattr(pig_module, "make_prescribe_node", lambda config: _prescribe())

    compiled = pig_module.build_pig_subgraph()
    trace = await _trace(compiled, _state(process_id=ProcessId.PIG, stage=ConsultStage.INTAKE))

    assert trace == ["report_guard_entry", "observe", "report_guard", "interview", "verify", "judge", "prescribe"]
    assert _order_ok(trace)


# ---- 4. 行数上限 ----


def _code_line_count(module) -> int:
    """统计模块去掉注释与空行后的有效行数。"""
    source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
    count = 0
    for line in source.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def test_phase1_subgraph_line_spread_within_limit() -> None:
    """五条子图的有效行数极差不超过 14：结构同构，体量不应漂移。"""
    modules = (rice_module, tea_module, chili_module, weed_module, pig_module)
    counts = [_code_line_count(module) for module in modules]
    assert max(counts) - min(counts) <= _MAX_LINE_SPREAD, dict(zip([m.__name__ for m in modules], counts, strict=True))
