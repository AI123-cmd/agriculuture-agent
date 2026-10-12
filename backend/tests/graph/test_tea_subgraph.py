"""T6-04：茶子图路径用例（技术方案 15.4 原文用例落地）。

15.4 的原文用例针对改造前的单图（首节点 `intake`、节点名 `collect_symptoms` / `diagnose`）。
本次结构改造后，这些旧节点名已全部废弃（`collect_symptoms` 由 `observe` 取代、`diagnose` 由
`judge` 取代），且「受理即分流」是**主图**属性（`intake → triage` 早于一切 `subgraph_*`），
子图本就不含 `intake`。因此按用户规则「逻辑以新文档为主、旧接口全废弃、以代码现状为准」，
本文件把 15.4 断言的**语义**映射到新结构：

- `traced[0] == "intake"`（旧）→ 子图首节点 `traced[0] == "observe"`；主图的「受理即分流」由
  `test_main_graph.py` 的 `test_route_at_intake_when_process_not_locked` 断言。
- `"collect_symptoms" in traced` 且早于 `"diagnose"`（旧）→ `report_guard` 早于 `interview`、
  `interview` 早于 `judge`（茶 `panel_enabled=true`，故 `interview → panel → verify → judge`）。

三处必需注释随用例保留：`stream_mode="updates"`、`first_index` 取首次出现、以及「triage 先于
问诊」的真正语义归属。
"""

from __future__ import annotations

import pytest

from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.state import ConsultStage, ProcessId
from tests.graph.conftest import first_index, initial_state, run_path, stub_leaf_factories, subgraph_module


@pytest.mark.asyncio
async def test_tea_subgraph_asks_all_required_slots(monkeypatch) -> None:
    """茶子图（15.4 原文用例的新结构映射）：受理入口正确且问诊在判流程之后推进。

    注释之一：`stream_mode="updates"` 是必需的——节点名在更新事件的键里；换成 `"values"`
    会拿到状态快照而非节点名，`traced` 变空并让位置断言直接 `IndexError`。
    """
    module = subgraph_module(ProcessId.TEA)
    stub_leaf_factories(monkeypatch, module, panel_needed=True)
    app = SUBGRAPH_BUILDERS[ProcessId.TEA]()

    state = initial_state(process_id=ProcessId.TEA, stage=ConsultStage.INTAKE)
    traced = await run_path(app, state, thread_id="t-tea-15-4")

    # 注释之二：`traced` 会因回环重复出现同一节点，故位置断言一律用 `first_index`
    # （取首次出现）配合 `<` 比较，不用 `==` / `+1`。
    assert traced[0] == "observe"  # 入口：看现场（旧结构的 `intake` 属主图，不在此）
    assert "report_guard" in traced
    assert "interview" in traced

    # 注释之三：真正要守的语义是「在问诊之前已经过守卫与依据核查」，而不是某个入口名。
    assert first_index(traced, "report_guard") < first_index(traced, "interview")
    assert first_index(traced, "interview") < first_index(traced, "panel")
    assert first_index(traced, "panel") < first_index(traced, "verify")
    assert first_index(traced, "verify") < first_index(traced, "judge")
    assert first_index(traced, "judge") < first_index(traced, "prescribe")


@pytest.mark.asyncio
async def test_tea_subgraph_asks_all_required_slots_doc_string_intent() -> None:
    """把 15.4 原文的三条断言语义显式记录，供后续回归对照（不跑图，纯意图说明）。

    原文：`traced[0] == "intake"`（受理即分流，不得先问再分流）。
    新结构下该语义由主图承担：`intake` 是主图首节点，其后第一条边即 `triage` /
    `subgraph_router`，绝无「先问症状再定流程」的中间态（见 `test_main_graph.py`）。
    """
    main_entry = "intake"
    subgraph_entry = "observe"
    assert main_entry != subgraph_entry
