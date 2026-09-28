"""T2-45：闸门子图的三态输出处理与 state → GateContext 压缩。

用例分四组：

1. **三态输出**：`allow` 保留原文本、`degrade` 整体替换为降级文案且不改写后输出、
   `block` 置 `BLOCKED`（不是 `HANDOFF`）并触发一次复核工单；
2. **影子模式**：`GateService` 返回 `shadowed` 外壳时，节点仍按内层真实判定渲染；
3. **context 压缩**：`_flatten_slots` 把 `unknown`/`missing` 归一为 `None`；`_history`
   跳过缺名/缺日期的行；红线快照缺失时构造"不可用"而非抛错；
4. **子图形态**：`build_gate_subgraph()` 无参与带参两种调用都返回已编译子图。

`GateService` 用桩替换：闸门的规则判定由 T2-34 单独覆盖，本项只验证节点对 decision 的
输出处理与工单触发，因此不牵动数据库（真实 `GateService` 会写审计表）。
"""

from datetime import date
from typing import Any

import pytest

from app.agent.contracts import FollowupPlan, PesticideItem, Prescription, PrescriptionAction, dump_contract
from app.agent.graph import gate_graph
from app.agent.state import ConsultStage
from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.contracts import GateResult, GateViolation
from app.process_config.redlines import LoadedRedlines, Redline
from app.process_config.rule_ids import GateRuleId

_PROCESS = "rice"


class _FakeKnowledge:
    """`KnowledgeFacts` 的最小实现：节点不调用它（桩服务不跑规则），仅满足构造。"""

    def get_entry(self, knowledge_id: str):
        return None

    def is_approved(self, knowledge_id: str) -> bool:
        return False

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ()


class _StubService:
    """把 `GateService.review` 固定返回一个预设结果，隔离出节点自身的行为。"""

    def __init__(self, result: GateResult) -> None:
        self._result = result
        self.calls: list[tuple[Any, Any]] = []

    async def review(self, draft: object, ctx: object) -> GateResult:
        self.calls.append((draft, ctx))
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
        process_id=_process_id(),
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


def _process_id():
    from app.agent.state import ProcessId

    return ProcessId(_PROCESS)


def _state(**overrides) -> dict:
    base: dict = {
        "session_id": "sess-0001",
        "user_id": "user-0001",
        "process_id": _PROCESS,
        "config_version": 1,
        "prescription": _prescription_dict(),
        "prescription_text": "建议在傍晚喷施吡虫啉，出现药害立即停用。",
        "redlines": _loaded_redlines_dict(),
        "slots": {},
        "prescription_history": [],
        "region_code": None,
    }
    base.update(overrides)
    return base


def _result(decision: str, **overrides) -> GateResult:
    base: dict = {
        "passed": decision == "allow",
        "violations": [],
        "checked_rules": ["R-REDLINE-LOAD"],
        "process_redlines_loaded": True,
        "decision": decision,
        "degrade_output": DEGRADE_OUTPUT_TEXT if decision == "degrade" else None,
    }
    if decision == "block":
        base["violations"] = [
            GateViolation(
                rule_id="R-WHITELIST", severity="block", message="不在已核准库", field="pesticide_items[K-1001]"
            )
        ]
    base.update(overrides)
    return GateResult(**base)


def _patch_service(monkeypatch, result: GateResult) -> _StubService:
    stub = _StubService(result)
    monkeypatch.setattr(gate_graph, "GateService", lambda *a, **k: stub)
    return stub


async def test_allow_keeps_original_text_and_gate_stage(monkeypatch) -> None:
    _patch_service(monkeypatch, _result("allow"))
    node = gate_graph.build_gate_node(_FakeKnowledge())

    out = await node(_state())

    assert out["prescription_text"] == "建议在傍晚喷施吡虫啉，出现药害立即停用。"
    assert out["stage"] == ConsultStage.GATE
    assert out.get("prescription_suppressed", False) is False


async def test_degrade_replaces_output_and_marks_suppressed(monkeypatch) -> None:
    _patch_service(monkeypatch, _result("degrade"))
    node = gate_graph.build_gate_node(_FakeKnowledge())

    out = await node(_state())

    assert out["prescription_text"] == DEGRADE_OUTPUT_TEXT
    assert out["stage"] == ConsultStage.GATE
    assert out["prescription_suppressed"] is True
    assert "吡虫啉" not in out["prescription_text"]


async def test_block_sets_blocked_stage_and_raises_ticket(monkeypatch) -> None:
    _patch_service(monkeypatch, _result("block"))
    tickets: list[dict] = []

    async def _fake_ticket(**kwargs) -> None:
        tickets.append(kwargs)

    monkeypatch.setattr(gate_graph, "enqueue_review_ticket", _fake_ticket)
    node = gate_graph.build_gate_node(_FakeKnowledge())

    out = await node(_state())

    assert out["stage"] == ConsultStage.BLOCKED
    assert out["stage"] != ConsultStage.HANDOFF
    assert out["prescription_text"] == DEGRADE_OUTPUT_TEXT
    assert out["prescription_suppressed"] is True
    assert len(tickets) == 1
    assert tickets[0]["knowledge_ids"] == ["K-1001"]
    assert tickets[0]["rule_ids"] == ["R-WHITELIST"]


async def test_allow_does_not_raise_ticket(monkeypatch) -> None:
    _patch_service(monkeypatch, _result("allow"))
    tickets: list[dict] = []

    async def _fake_ticket(**kwargs) -> None:
        tickets.append(kwargs)

    monkeypatch.setattr(gate_graph, "enqueue_review_ticket", _fake_ticket)
    node = gate_graph.build_gate_node(_FakeKnowledge())

    await node(_state())

    assert tickets == []


async def test_shadow_shell_routes_on_inner_decision(monkeypatch) -> None:
    """影子模式下外层是放行外壳，节点仍按 `shadowed.decision` 处理（T2-46）。"""
    inner = _result("block")
    _patch_service(monkeypatch, GateResult(passed=True, shadowed=inner))
    tickets: list[dict] = []

    async def _fake_ticket(**kwargs) -> None:
        tickets.append(kwargs)

    monkeypatch.setattr(gate_graph, "enqueue_review_ticket", _fake_ticket)
    node = gate_graph.build_gate_node(_FakeKnowledge())

    out = await node(_state())

    assert out["stage"] == ConsultStage.BLOCKED
    assert out["gate_result"]["decision"] == "block"
    assert len(tickets) == 1


async def test_degrade_does_not_mutate_prescription(monkeypatch) -> None:
    _patch_service(monkeypatch, _result("degrade"))
    node = gate_graph.build_gate_node(_FakeKnowledge())
    state = _state()
    before = dict(state["prescription"])

    await node(state)

    assert state["prescription"] == before


def test_build_gate_context_normalizes_unknown_and_missing_to_none() -> None:
    state = _state(
        slots={
            "answered": {"key": "answered", "value": "已答", "status": "filled", "source": "user_answer"},
            "unanswerable": {"key": "unanswerable", "value": None, "status": "unknown", "source": "user_answer"},
            "not_asked": {"key": "not_asked", "value": None, "status": "missing", "source": "inferred"},
        }
    )

    ctx = gate_graph.build_gate_context(state, knowledge=_FakeKnowledge(), today=date(2025, 6, 1))

    assert ctx.slots["answered"] == "已答"
    assert ctx.slots["unanswerable"] is None
    assert ctx.slots["not_asked"] is None


def test_build_gate_context_missing_redlines_is_unavailable_not_error() -> None:
    """红线快照缺失时构造"不可用"，让引擎的 R-REDLINE-LOAD 走 fail-closed，而非抛错。"""
    ctx = gate_graph.build_gate_context(_state(redlines=None), knowledge=_FakeKnowledge(), today=date(2025, 6, 1))

    assert ctx.redlines.loaded is False


def test_build_gate_context_parses_history_and_skips_incomplete_rows() -> None:
    state = _state(
        prescription_history=[
            {"product": "吡虫啉", "used_on": "2025-05-01"},
            {"pesticide_name": "阿维菌素", "date": "2025-05-10"},
            {"product": "无日期药剂"},
            {"used_on": "2025-05-11"},
            "not-a-mapping",
        ]
    )

    ctx = gate_graph.build_gate_context(state, knowledge=_FakeKnowledge(), today=date(2025, 6, 1))

    assert [r.pesticide_name for r in ctx.medication_history] == ["吡虫啉", "阿维菌素"]
    assert all(r.used_on is not None for r in ctx.medication_history)


def test_build_gate_subgraph_returns_compiled_with_and_without_knowledge() -> None:
    compiled_noarg = gate_graph.build_gate_subgraph()
    compiled_arg = gate_graph.build_gate_subgraph(_FakeKnowledge())

    assert "gate_check" in compiled_noarg.get_graph().nodes
    assert "gate_check" in compiled_arg.get_graph().nodes


def test_rule_configs_merges_bounds_conservatively() -> None:
    """同一 rule_id 被多条红线声明时，上限取更小、下限取更大（更严的一侧）。"""
    redlines = LoadedRedlines(
        process_id=_process_id(),
        version=1,
        redlines=(
            Redline(rule_ids=(GateRuleId.MAX_USE,), id="A", desc="上限", severity="block", payload={"limit": 3}),
            Redline(rule_ids=(GateRuleId.MAX_USE,), id="B", desc="上限", severity="block", payload={"limit": 1}),
            Redline(rule_ids=(GateRuleId.INTERVAL,), id="C", desc="下限", severity="block", payload={"min_days": 7}),
            Redline(rule_ids=(GateRuleId.INTERVAL,), id="D", desc="下限", severity="block", payload={"min_days": 14}),
        ),
        loaded=True,
    )

    configs = gate_graph._rule_configs(redlines)

    assert configs["R-MAX-USE"]["limit"] == 1
    assert configs["R-INTERVAL"]["min_days"] == 14


@pytest.mark.parametrize("bad_rows", [[{"product": "药"}], [{"used_on": "2025-05-01"}], ["x"]])
def test_history_skips_rows_without_name_or_date(bad_rows: list) -> None:
    assert gate_graph._history(bad_rows) == []
