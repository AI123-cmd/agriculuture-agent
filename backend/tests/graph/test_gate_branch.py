"""T6-04：闸门三条出边的路径断言 + 影子模式按内层判定渲染。

`test_main_graph.py` 已用闸门子图替身覆盖三条出边（allow→followup、degrade/block→handoff）。本
文件补的是**真实闸门子图结构**下的独立验收点：影子模式（`GATE_MODE=shadow`）下
`GateService.review` 返回 `passed=True` 的放行外壳、真实判定在内层 `shadowed`，图上仍必须按
`shadowed.decision` 决定渲染与走向——避免把「影子模式」理解成「农户看到被拦截后的处方」。

用最小包装图（`gate` 子图 + `route_after_gate` + `followup`/`handoff`）驱动，`GateService` 用桩
替换（真实规则判定由 T6-03 与 T2 规则测试覆盖）：

- `allow` 的 gate 节点不中断：`astream` 可直接看到 `["gate", "followup"]`；
- `degrade` / `block` 的 gate 节点会 `interrupt`（安全拦截后确认），故用 `ainvoke` + `Command`
  恢复跑完，断言终点落到 `handoff` 且不经过 `followup`。
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.contracts import FollowupPlan, PesticideItem, Prescription, PrescriptionAction, dump_contract
from app.agent.graph import gate_graph
from app.agent.graph.routes import ROUTE_AFTER_GATE, route_after_gate
from app.agent.state import ConsultStage, ConsultationState, ProcessId
from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.contracts import GateResult, GateViolation
from app.process_config.redlines import LoadedRedlines, Redline
from app.process_config.rule_ids import GateRuleId
from tests.graph.conftest import memory_checkpointer, run_path

_PROCESS = ProcessId.RICE


class _FakeKnowledge:
    """`KnowledgeFacts` 最小实现：桩服务不跑规则，仅满足构造。"""

    def get_entry(self, knowledge_id: str) -> None:
        return None

    def is_approved(self, knowledge_id: str) -> bool:
        return False

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ()


class _StubService:
    """固定返回预设 `GateResult`，隔离出「图上按哪条判定走」。"""

    def __init__(self, result: GateResult) -> None:
        self._result = result

    async def review(self, draft: object, ctx: object) -> GateResult:
        return self._result


def _prescription_dict() -> dict:
    prescription = Prescription(
        diagnosis_summary="水稻稻飞虱若虫危害",
        actions=[
            PrescriptionAction(
                order=1,
                action="建议喷施吡虫啉",
                timing="晴天傍晚",
                method="叶面均匀喷雾",
                cautions=["避免高温施药"],
                stop_conditions=["出现药害斑立即停用"],
            )
        ],
        safety_interval_days=7,
        max_uses_per_season=2,
        pesticide_items=[
            PesticideItem(
                knowledge_id="K-1001",
                name="吡虫啉",
                dosage_min=15.0,
                dosage_max=20.0,
                safety_interval_days=7,
                max_uses_per_season=2,
            )
        ],
        soil_level_actions=[],
        followup_plan=FollowupPlan(tasks=[]),
        facts_provenance={
            "safety_interval_days": "knowledge:K-1001",
            "max_uses_per_season": "knowledge:K-1001",
            "pesticide_items[0].dosage_min": "knowledge:K-1001:field:dosage_min",
            "pesticide_items[0].dosage_max": "knowledge:K-1001:field:dosage_max",
            "pesticide_items[0].safety_interval_days": "knowledge:K-1001",
            "pesticide_items[0].max_uses_per_season": "knowledge:K-1001",
        },
        requires_tech_station=False,
        detail_terms={},
    )
    return dump_contract(prescription)


def _loaded_redlines_dict() -> dict:
    return LoadedRedlines(
        process_id=_PROCESS,
        version=1,
        redlines=(
            Redline(
                rule_ids=(GateRuleId.REDLINE,),
                id="RICE-01",
                desc="禁用清单",
                severity="block",
                payload={"forbidden": ["某某禁用农药"]},
            ),
        ),
        loaded=True,
    ).model_dump(mode="json")


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-gate-branch",
        "user_id": "user-gate-branch",
        "process_id": _PROCESS,
        "config_version": 1,
        "prescription": _prescription_dict(),
        "prescription_text": "建议在傍晚喷施吡虫啉。",
        "redlines": _loaded_redlines_dict(),
        "slots": {},
        "prescription_history": [],
        "region_code": None,
        "messages": [],
    }
    base.update(overrides)
    return base


def _allow_result() -> GateResult:
    return GateResult(passed=True, checked_rules=["R-REDLINE-LOAD"], process_redlines_loaded=True, decision="allow")


def _non_allow_result(decision: str) -> GateResult:
    violations = (
        [GateViolation(rule_id="R-WHITELIST", severity="block", message="不在已核准库", field="pesticide_items")]
        if decision == "block"
        else []
    )
    return GateResult(
        passed=False,
        violations=violations,
        checked_rules=["R-REDLINE-LOAD"],
        process_redlines_loaded=True,
        decision=decision,
        degrade_output=DEGRADE_OUTPUT_TEXT,
    )


def _gate_app(monkeypatch, result: GateResult):
    """最小闸门图：真实 gate 子图结构 + 桩服务，出边为 followup / handoff。"""
    monkeypatch.setattr(gate_graph, "GateService", lambda *a, **k: _StubService(result))

    async def _noop_ticket(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(gate_graph, "enqueue_review_ticket", _noop_ticket)

    async def followup(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.FOLLOWUP}

    async def handoff(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.HANDOFF}

    graph = StateGraph(ConsultationState)
    graph.add_node("gate", gate_graph.build_gate_subgraph(_FakeKnowledge()))
    graph.add_node("followup", followup)
    graph.add_node("handoff", handoff)
    graph.add_edge(START, "gate")
    graph.add_conditional_edges("gate", route_after_gate, ROUTE_AFTER_GATE)
    graph.add_edge("followup", END)
    graph.add_edge("handoff", END)
    return graph.compile(checkpointer=memory_checkpointer())


async def _run_through_interrupt(app: Any, state: dict, thread_id: str, *, resume: str = "continue") -> dict:
    """跑一遍；若在 gate 中断则恢复，返回最终 state。"""
    config = {"configurable": {"thread_id": thread_id}}
    first = await app.ainvoke(state, config)
    if first.get("__interrupt__"):
        return cast(dict, await app.ainvoke(Command(resume=resume), config))
    return first


@pytest.mark.asyncio
async def test_gate_allow_reaches_followup_not_handoff(monkeypatch) -> None:
    """allow：gate → followup，路径里没有 handoff。"""
    app = _gate_app(monkeypatch, _allow_result())

    traced = await run_path(app, _state(), thread_id="t-gate-allow")

    assert traced == ["gate", "followup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["degrade", "block"])
async def test_gate_non_allow_reaches_handoff_not_followup(monkeypatch, decision: str) -> None:
    """degrade / block 都转人工：经中断-恢复后终点 stage 为 HANDOFF，不经过 followup。"""
    app = _gate_app(monkeypatch, _non_allow_result(decision))

    out = await _run_through_interrupt(app, _state(), f"t-gate-{decision}")

    assert out["stage"] == ConsultStage.HANDOFF
    assert out["stage"] != ConsultStage.FOLLOWUP
    assert out["prescription_suppressed"] is True


@pytest.mark.asyncio
async def test_shadow_mode_renders_on_inner_decision(monkeypatch) -> None:
    """影子模式：外层 `passed=True` 放行外壳，真实判定在内层 `shadowed`。

    闸门节点必须按 `shadowed.decision` 处理——block 时仍走 handoff、处方被抑制，
    而不是当作 allow 走 followup 把处方放给农户。
    """
    inner_block = _non_allow_result("block")
    shell = GateResult(passed=True, shadowed=inner_block)
    app = _gate_app(monkeypatch, shell)

    out = await _run_through_interrupt(app, _state(), "t-gate-shadow")

    assert out["stage"] == ConsultStage.HANDOFF  # 内层 block：不放行
    assert out["gate_result"]["decision"] == "block"
    assert out["prescription_suppressed"] is True


def test_shadow_shell_decision_is_the_inner_one() -> None:
    """纯契约核对：`shadowed` 非空时 `effective` 取内层，与闸门节点实现同源。"""
    inner = _non_allow_result("degrade")
    shell = GateResult(passed=True, shadowed=inner)

    effective = shell.shadowed if shell.shadowed is not None else shell

    assert effective.decision == "degrade"
    assert shell.passed is True  # 外层仍是放行外壳（只影响是否拦截，不改变渲染口径）


def test_gate_context_reads_state_without_conversation_fields() -> None:
    """闸门上下文的 state→GateContext 压缩可用，且红线缺失时按 fail-closed 构造。"""
    ctx = gate_graph.build_gate_context(_state(redlines=None), knowledge=_FakeKnowledge(), today=date(2025, 6, 1))

    assert ctx.redlines.loaded is False
