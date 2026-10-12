"""T6-04：七条流程子图的路径正确性（逐条参数化，不抽代表）。

技术方案 7.4 明确「每条流程一个子图，子图结构与主链路一致，差异全部来自配置」，因此一旦
某个子图文件漏注册节点或条件边写错，其他流程仍会通过；抽一条代表来测会漏掉。本文件对
`SUBGRAPH_BUILDERS` 里的**七条**流程逐条跑一遍并断言节点访问顺序。

本阶段文档引用的旧节点名（子图首节点为 `intake`、`collect_symptoms` 先于 `diagnose` 等）在当前
代码中不存在：新结构里子图入口是 `observe`（养猪是入口守卫 `report_guard_entry`），受理即分流
是主图属性（见 `test_main_graph.py`）。此处一律以代码现状为准，断言子图真实的节点序：
入口 → `observe` → `report_guard` → `interview` →(`panel`)→ `verify` → `judge` → `prescribe`。

会诊（`panel`）只在茶 / 辣椒 / 杂草启用，故这三条额外断言 `interview < panel < verify`。
"""

from __future__ import annotations

import pytest

from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.state import ConsultStage, ProcessId
from tests.graph.conftest import first_index, initial_state, run_path, stub_leaf_factories, subgraph_module

# 会诊启用的三条流程，与 `routes.PANEL_ENABLED_PROCESSES` 对齐。
_PANEL_FLOWS = (ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED)

_FLOWS = sorted(SUBGRAPH_BUILDERS, key=lambda process_id: process_id.value)


def test_registry_covers_exactly_the_seven_flows() -> None:
    """七值枚举与注册表一一对应，不多不少。"""
    assert set(SUBGRAPH_BUILDERS) == set(ProcessId)


def test_tobacco_flow_is_not_registered() -> None:
    """烟草（产品说明书附录 A9）前提未定，不注册、不假装可用。

    `ProcessId` 无 `tobacco` 值，本用例把「刻意没有它」钉住；一旦有人补上该枚举却漏了配置，
    上面的一致性用例会立刻失败。
    """
    assert {process_id.value for process_id in ProcessId} == {
        "rice",
        "tea",
        "rapeseed",
        "chili",
        "weed",
        "pig",
        "general",
    }


@pytest.mark.parametrize("process_id", _FLOWS, ids=lambda process_id: process_id.value)
async def test_subgraph_path(monkeypatch, process_id: ProcessId) -> None:
    """每条子图：入口正确，且 observe < report_guard < interview < judge < prescribe。"""
    module = subgraph_module(process_id)
    panel = process_id in _PANEL_FLOWS
    stub_leaf_factories(monkeypatch, module, panel_needed=panel)
    app = SUBGRAPH_BUILDERS[process_id]()

    # 养猪的入口守卫按 stage 区分去向，故初始 stage 置为 INTAKE 让它先 `observe`。
    state = initial_state(process_id=process_id, stage=ConsultStage.INTAKE)
    traced = await run_path(app, state, thread_id=f"t-subgraph-{process_id.value}")

    entry = "report_guard_entry" if process_id is ProcessId.PIG else "observe"
    assert traced[0] == entry, f"{process_id.value}: 首节点应为 {entry}，实得 {traced[:3]}"

    # 至少四条路径断言（文档要求每条流程 ≥ 4）。
    assert first_index(traced, "observe") < first_index(traced, "report_guard")
    assert first_index(traced, "report_guard") < first_index(traced, "interview")
    assert first_index(traced, "interview") < first_index(traced, "judge")
    assert first_index(traced, "judge") < first_index(traced, "prescribe")

    if panel:
        assert first_index(traced, "interview") < first_index(traced, "panel") < first_index(traced, "verify")
    else:
        assert "panel" not in traced

    assert traced[-1] == "prescribe", f"{process_id.value}: 正常终点应为 prescribe，实得 {traced[-1]}"
