"""T2-24：九条条件边的路由判定与四条硬性约束。

用例分五组：

1. **主图入口与分诊**：`route_after_intake` 的直达/判流程两分支；`route_after_triage` 的
   报告信号、对象确认、进流程三分支；`route_after_confirm_object` 的冲突回分诊与未定流程抛错；
   `route_after_report_guard` 的三分支；`route_after_subgraph` 只有 `stage=PRESCRIBE` 才进闸门；
2. **子图内路由**：`route_after_subgraph_guard` 按 `stage` 区分入口守卫与兜底守卫；
   `route_after_interview` 的守卫、会诊、槽位齐备、未收敛回环、预算耗尽五分支；
   `route_after_judge` 的超边界、补问、开方、未覆盖四分支；
3. **fail-closed**：`redlines_loaded=False` 时九条边全部返回 `handoff`，一条都不放行；
4. **未覆盖即抛错**：`UnroutedStateError` 的消息含函数名，不回退到任何 `default` 分支；
5. **path_map 与边归属**：四个子图 path_map 的 `handoff` 指向 `"__end__"`，主图的两条边
   指向节点名——LangGraph 不允许子图的边指向父图节点。

用例全部是纯内存构造的 dict，不声明 `engine`，因此不会被"需要 PostgreSQL"跳过。
"""

from typing import Any, cast

import pytest

from app.agent.graph.routes import (
    PANEL_ENABLED_PROCESSES,
    ROUTE_AFTER_CONFIRM_OBJECT,
    ROUTE_AFTER_GATE,
    ROUTE_AFTER_INTAKE,
    ROUTE_AFTER_INTERVIEW,
    ROUTE_AFTER_JUDGE,
    ROUTE_AFTER_REPORT_GUARD,
    ROUTE_AFTER_SUBGRAPH,
    ROUTE_AFTER_SUBGRAPH_GUARD,
    ROUTE_AFTER_TRIAGE,
    ROUTE_MAPS,
    UnroutedStateError,
    route_after_confirm_object,
    route_after_gate,
    route_after_intake,
    route_after_interview,
    route_after_judge,
    route_after_report_guard,
    route_after_subgraph,
    route_after_subgraph_guard,
    route_after_triage,
    slots_ready,
)
from app.agent.state import ConsultStage, ConsultationState, ProcessId

_ALL_ROUTES = (
    route_after_intake,
    route_after_triage,
    route_after_confirm_object,
    route_after_report_guard,
    route_after_subgraph_guard,
    route_after_subgraph,
    route_after_interview,
    route_after_judge,
    route_after_gate,
)

# 以 `state["redlines_loaded"]` 开头做 fail-closed 的八条边。`route_after_gate` 不在其中：
# 它的权威来源是门禁结论上的 `process_redlines_loaded`（闸门自己会写），由下面的专项用例覆盖。
_REDLINE_GUARDED_ROUTES = tuple(route for route in _ALL_ROUTES if route is not route_after_gate)


def _state(**overrides: Any) -> ConsultationState:
    """构造一份"红线已加载、流程未定"的基线 state，用例只覆盖关心的键。"""
    base: dict[str, Any] = {"redlines_loaded": True}
    base.update(overrides)
    return cast(ConsultationState, base)


def _slots(**status_by_key: str) -> dict[str, dict[str, Any]]:
    """按 `键=状态` 造槽位。`filled` 时补一个值，避免状态与值不一致。"""
    return {
        key: {"key": key, "status": status, "value": key if status == "filled" else None}
        for key, status in status_by_key.items()
    }


# ---------------------------------------------------------------- 受理与分诊


def test_intake_with_locked_process_enters_subgraph() -> None:
    """入口直达（小程序按钮带 process_hint）已锁定流程，不再走 triage。"""
    state = _state(process_locked=True, process_id=ProcessId.WEED)
    assert route_after_intake(state) == "enter_process"


def test_intake_locked_without_process_id_falls_back_to_triage() -> None:
    """`process_locked=True` 却拿不到 `process_id` 时不能进子图——子图选不出来。"""
    assert route_after_intake(_state(process_locked=True)) == "triage"


def test_intake_without_hint_goes_to_triage() -> None:
    """受理即分流：没锁定时第二条边就去判流程，不存在"先问症状"的中间态。"""
    assert route_after_intake(_state(process_locked=False)) == "triage"


def test_triage_report_signal_goes_to_entry_guard() -> None:
    """信号命中优先于对象确认：先过守卫再谈其他。"""
    state = _state(triage={"handoff_to_report": True, "object_confirmed": False})
    assert route_after_triage(state) == "report_guard"


def test_triage_unconfirmed_object_asks_for_confirmation() -> None:
    state = _state(triage={"handoff_to_report": False, "object_confirmed": False})
    assert route_after_triage(state) == "confirm_object"


def test_triage_pig_still_enters_subgraph() -> None:
    """养猪的 pre-observe 检查在子图入口守卫里，故 triage 对 PIG 仍进子图。"""
    state = _state(
        triage={"handoff_to_report": False, "object_confirmed": True},
        process_id=ProcessId.PIG,
    )
    assert route_after_triage(state) == "enter_process"


def test_triage_without_result_asks_for_confirmation() -> None:
    """`triage` 缺失时按"对象未确认"处理，而不是直接进子图。"""
    assert route_after_triage(_state()) == "confirm_object"


# ------------------------------------------------------------ 确认回环与出口


def test_confirm_object_conflict_goes_back_to_triage() -> None:
    """农户答复与档案冲突只能回分诊重判，不能就地改流程。"""
    state = _state(object_confirmed_conflict=True, process_id=ProcessId.RICE)
    assert route_after_confirm_object(state) == "triage"


def test_confirm_object_resolved_enters_subgraph() -> None:
    state = _state(process_id=ProcessId.CHILI)
    assert route_after_confirm_object(state) == "enter_process"


def test_confirm_object_without_process_id_raises() -> None:
    """未定流程又未冲突，说明确认节点漏写了 `process_id`，必须当场暴露。"""
    with pytest.raises(UnroutedStateError) as excinfo:
        route_after_confirm_object(_state())
    assert "route_after_confirm_object" in str(excinfo.value)


def test_report_guard_hit_goes_to_handoff() -> None:
    state = _state(report_decision={"stop_consultation": True, "triggered": True})
    assert route_after_report_guard(state) == "handoff"


def test_report_guard_triggered_alone_also_stops() -> None:
    """`triggered` 与 `stop_consultation` 任一为真都拦：两个字段分属契约与产品语义。"""
    state = _state(report_decision={"triggered": True, "stop_consultation": False})
    assert route_after_report_guard(state) == "handoff"


def test_report_guard_pass_without_process_asks_confirmation() -> None:
    state = _state(report_decision={"stop_consultation": False, "triggered": False})
    assert route_after_report_guard(state) == "confirm_object"


def test_report_guard_pass_with_process_enters_subgraph() -> None:
    state = _state(process_id=ProcessId.RICE, report_decision={"triggered": False, "stop_consultation": False})
    assert route_after_report_guard(state) == "enter_process"


def test_subgraph_exit_at_prescribe_enters_gate() -> None:
    state = _state(stage=ConsultStage.PRESCRIBE, diagnosis={"out_of_scope": False})
    assert route_after_subgraph(state) == "gate"


@pytest.mark.parametrize(
    "stage",
    [
        ConsultStage.INTAKE,
        ConsultStage.TRIAGE,
        ConsultStage.OBSERVE,
        ConsultStage.INTERVIEW,
        ConsultStage.VERIFY,
        ConsultStage.JUDGE,
        ConsultStage.GATE,
        ConsultStage.FOLLOWUP,
        ConsultStage.HANDOFF,
        ConsultStage.BLOCKED,
    ],
)
def test_subgraph_exit_before_prescribe_goes_to_handoff(stage: ConsultStage) -> None:
    """只有走到 prescribe 算正常完成，其余十余种提前退出原因一律转人工。"""
    state = _state(stage=stage, diagnosis={"out_of_scope": False})
    assert route_after_subgraph(state) == "handoff"


def test_subgraph_exit_on_out_of_scope_skips_gate() -> None:
    """超边界时即使 stage 已是 PRESCRIBE 也不进闸门——闸门要的处方根本不存在。"""
    state = _state(stage=ConsultStage.PRESCRIBE, diagnosis={"out_of_scope": True})
    assert route_after_subgraph(state) == "handoff"


def test_subgraph_exit_on_guard_hit_goes_to_handoff() -> None:
    state = _state(
        stage=ConsultStage.PRESCRIBE,
        diagnosis={"out_of_scope": False},
        report_decision={"triggered": True, "stop_consultation": True},
    )
    assert route_after_subgraph(state) == "handoff"


# -------------------------------------------------------------- 子图内守卫


def test_subgraph_guard_hit_leaves_subgraph() -> None:
    state = _state(report_decision={"triggered": True, "stop_consultation": True})
    assert route_after_subgraph_guard(state) == "handoff"


@pytest.mark.parametrize("stage", [ConsultStage.INTAKE, ConsultStage.TRIAGE])
def test_subgraph_guard_before_observe_lets_pig_see_the_field(stage: ConsultStage) -> None:
    """养猪的入口守卫在 observe 之前，放行后去看现场。"""
    assert route_after_subgraph_guard(_state(stage=stage)) == "observe"


@pytest.mark.parametrize(
    "stage",
    [ConsultStage.OBSERVE, ConsultStage.INTERVIEW, ConsultStage.VERIFY, ConsultStage.JUDGE],
)
def test_subgraph_guard_after_observe_goes_to_interview(stage: ConsultStage) -> None:
    """其余流程的守卫在 observe 之后，放行后进问诊。"""
    assert route_after_subgraph_guard(_state(stage=stage)) == "interview"


def test_subgraph_guard_without_stage_goes_to_interview() -> None:
    assert route_after_subgraph_guard(_state()) == "interview"


# ------------------------------------------------------------------ 问诊路由


def test_interview_guard_hit_stops() -> None:
    state = _state(report_decision={"triggered": True, "stop_consultation": True})
    assert route_after_interview(state) == "handoff"


def test_interview_ready_slots_go_to_verify() -> None:
    state = _state(
        process_id=ProcessId.RICE,
        required_slots=["seedling_stage"],
        slots=_slots(seedling_stage="filled"),
    )
    assert route_after_interview(state) == "verify"


@pytest.mark.parametrize("process_id", sorted(PANEL_ENABLED_PROCESSES, key=lambda item: item.value))
def test_interview_panel_enabled_process_with_need_goes_to_panel(process_id: ProcessId) -> None:
    state = _state(
        process_id=process_id,
        panel_needed=True,
        required_slots=["symptom"],
        slots=_slots(symptom="filled"),
    )
    assert route_after_interview(state) == "panel"


def test_interview_panel_enabled_process_without_need_goes_to_verify() -> None:
    """会诊由 `panel_needed` 决定，不按流程无条件启用。"""
    state = _state(
        process_id=ProcessId.TEA,
        panel_needed=False,
        required_slots=["symptom"],
        slots=_slots(symptom="filled"),
    )
    assert route_after_interview(state) == "verify"


def test_interview_non_panel_process_never_goes_to_panel() -> None:
    """水稻判别依据确定，即使 `panel_needed` 被误置为真也不进会诊。"""
    state = _state(
        process_id=ProcessId.RICE,
        panel_needed=True,
        required_slots=["symptom"],
        slots=_slots(symptom="filled"),
    )
    assert route_after_interview(state) == "verify"


def test_interview_incomplete_slots_asks_again() -> None:
    state = _state(
        required_slots=["symptom"],
        slots=_slots(symptom="missing"),
        budget={"interview_rounds": 1, "max_interview_rounds": 4},
    )
    assert route_after_interview(state) == "interview"


def test_interview_budget_exhausted_goes_to_handoff() -> None:
    state = _state(
        required_slots=["symptom"],
        slots=_slots(symptom="missing"),
        budget={"interview_rounds": 4, "max_interview_rounds": 4},
    )
    assert route_after_interview(state) == "handoff"


def test_interview_budget_defaults_are_used_when_absent() -> None:
    """`budget` 缺失时默认上限 4：第 0 轮仍可继续问，不会当场转人工。"""
    state = _state(required_slots=["symptom"], slots=_slots(symptom="missing"))
    assert route_after_interview(state) == "interview"


# ------------------------------------------------------------------ 判定路由


def test_judge_out_of_scope_goes_to_handoff() -> None:
    state = _state(diagnosis={"out_of_scope": True})
    assert route_after_judge(state) == "handoff"


def test_judge_likely_prescribes() -> None:
    state = _state(diagnosis={"out_of_scope": False, "confidence_level": "likely"})
    assert route_after_judge(state) == "prescribe"


def test_judge_preliminary_prescribes() -> None:
    state = _state(diagnosis={"out_of_scope": False, "confidence_level": "preliminary"})
    assert route_after_judge(state) == "prescribe"


def test_judge_uncertain_with_more_info_interviews_again() -> None:
    state = _state(
        diagnosis={"out_of_scope": False, "confidence_level": "uncertain", "needs_more_info": ["叶片背面"]},
        budget={"judge_loops": 0, "max_judge_loops": 2},
    )
    assert route_after_judge(state) == "interview"


def test_judge_uncertain_with_loops_exhausted_goes_to_handoff() -> None:
    state = _state(
        diagnosis={"out_of_scope": False, "confidence_level": "uncertain", "needs_more_info": ["叶片背面"]},
        budget={"judge_loops": 2, "max_judge_loops": 2},
    )
    assert route_after_judge(state) == "handoff"


def test_judge_uncertain_without_more_info_raises() -> None:
    """`uncertain` 却没写出还缺什么，属于判定契约违约，路由不得兜底成开方。"""
    state = _state(diagnosis={"out_of_scope": False, "confidence_level": "uncertain", "needs_more_info": []})
    with pytest.raises(UnroutedStateError) as excinfo:
        route_after_judge(state)
    assert "route_after_judge" in str(excinfo.value)


def test_judge_without_diagnosis_raises() -> None:
    with pytest.raises(UnroutedStateError):
        route_after_judge(_state())


# ------------------------------------------------------------------ 闸门路由


def test_gate_allow_goes_to_followup() -> None:
    state = _state(gate_result={"process_redlines_loaded": True, "decision": "allow"})
    assert route_after_gate(state) == "followup"


@pytest.mark.parametrize("decision", ["degrade", "block"])
def test_gate_non_allow_goes_to_handoff(decision: str) -> None:
    state = _state(gate_result={"process_redlines_loaded": True, "decision": decision})
    assert route_after_gate(state) == "handoff"


def test_gate_redlines_missing_fails_closed() -> None:
    """门禁结论是权威来源：state 上 `redlines_loaded=True` 也压不过闸门的"红线未加载"。"""
    state = _state(redlines_loaded=True, gate_result={"process_redlines_loaded": False, "decision": "allow"})
    assert route_after_gate(state) == "handoff"


def test_gate_unknown_decision_raises() -> None:
    state = _state(gate_result={"process_redlines_loaded": True, "decision": "escalate"})
    with pytest.raises(UnroutedStateError) as excinfo:
        route_after_gate(state)
    assert "route_after_gate" in str(excinfo.value)


# -------------------------------------------------------- 约束一：fail-closed


@pytest.mark.parametrize("route", _REDLINE_GUARDED_ROUTES, ids=lambda route: route.__name__)
def test_all_routes_fail_closed_without_redlines(route: Any) -> None:
    """红线未加载时八条边一律转人工；一个分支都不许放行。

    这条约束的失败模式最严重：用通用规则代替流程红线继续出方，等于突破产品说明书 9.3。
    用例刻意传入"除红线外一切都齐全"的 state，确保返回 `handoff` 只因红线缺失。
    """
    state = _state(
        redlines_loaded=False,
        process_locked=True,
        process_id=ProcessId.RICE,
        stage=ConsultStage.PRESCRIBE,
        required_slots=["symptom"],
        slots=_slots(symptom="filled"),
        panel_needed=True,
        triage={"handoff_to_report": False, "object_confirmed": True},
        diagnosis={"out_of_scope": False, "confidence_level": "likely"},
        report_decision={"triggered": False, "stop_consultation": False},
        gate_result={"process_redlines_loaded": True, "decision": "allow"},
    )
    assert route(state) == "handoff"


def test_gate_route_ignores_state_snapshot_and_trusts_the_gate() -> None:
    """`route_after_gate` 不以 state 快照为准：闸门是 fail-closed 组件，它的判定必须压过入口检查。"""
    state = _state(
        redlines_loaded=True,
        gate_result={"process_redlines_loaded": False, "decision": "allow"},
    )
    assert route_after_gate(state) == "handoff"


def test_fail_closed_check_is_the_first_statement() -> None:
    """顺序也重要：先判红线再判业务分支，否则"红线缺失 + 业务分支命中"会放行。"""
    state = _state(redlines_loaded=False, process_locked=True, process_id=ProcessId.RICE)
    assert route_after_intake(state) == "handoff"


# ------------------------------------------------------ 约束二：path_map 覆盖


def test_unrouted_error_carries_function_name_and_snapshot() -> None:
    """异常消息要能直接定位到函数与关键字段，检查点回溯时不必再猜。"""
    with pytest.raises(UnroutedStateError) as excinfo:
        route_after_gate(_state(stage=ConsultStage.GATE, gate_result={"process_redlines_loaded": True}))
    error = excinfo.value
    assert error.func_name == "route_after_gate"
    assert "route_after_gate" in str(error)
    assert "stage" in error.state_snapshot


def test_every_route_is_bound_to_a_path_map() -> None:
    """`ROUTE_MAPS` 是"新增路由漏接 path_map"的机器化兜底。"""
    assert {route.__name__ for route, _ in ROUTE_MAPS} == {route.__name__ for route in _ALL_ROUTES}


# -------------------------------------------------- 约束四：子图出口 vs 主图


def test_subgraph_path_maps_point_handoff_at_the_subgraph_exit() -> None:
    """LangGraph 不允许子图的边指向父图节点，四个子图 path_map 必须指向 `__end__`。"""
    for path_map in (ROUTE_AFTER_SUBGRAPH_GUARD, ROUTE_AFTER_INTERVIEW, ROUTE_AFTER_JUDGE):
        assert path_map["handoff"] == "__end__"


def test_main_graph_path_maps_point_handoff_at_the_handoff_node() -> None:
    """主图边相反：`handoff` 是主图节点，子图退出的统一归宿由主图决定。"""
    for path_map in (ROUTE_AFTER_INTAKE, ROUTE_AFTER_TRIAGE, ROUTE_AFTER_CONFIRM_OBJECT, ROUTE_AFTER_REPORT_GUARD):
        assert path_map["handoff"] == "handoff"
    assert ROUTE_AFTER_SUBGRAPH["handoff"] == "handoff"
    assert ROUTE_AFTER_GATE["handoff"] == "handoff"


def test_path_map_targets_are_consistent_with_route_names() -> None:
    """`ROUTE_AFTER_SUBGRAPH` 是本组唯一的子图出口边，指向主图的 `gate`。"""
    assert set(ROUTE_AFTER_SUBGRAPH) == {"gate", "handoff"}


# ------------------------------------------------------------------ slots_ready


def test_slots_ready_true_when_required_all_filled() -> None:
    state = _state(required_slots=["a", "b"], slots=_slots(a="filled", b="filled"))
    assert slots_ready(state) is True


def test_slots_ready_treats_unknown_as_resolved() -> None:
    """`unknown`（问过且答不出）是合法答案；当成缺口会让流程永远收敛不了。"""
    state = _state(required_slots=["a", "b"], slots=_slots(a="filled", b="unknown"))
    assert slots_ready(state) is True


@pytest.mark.parametrize("status", ["missing", "filled"])
def test_slots_ready_false_when_slot_absent_or_missing(status: str) -> None:
    state = _state(required_slots=["a", "b"], slots=_slots(a=status))
    assert slots_ready(state) is False


def test_slots_ready_true_without_required_slots() -> None:
    """没有必填槽位的流程（或 intake 尚未写清单）不阻塞推进。"""
    assert slots_ready(_state()) is True


def test_slots_ready_ignores_extra_slots() -> None:
    """非必填槽位不应进入判断——它不是 `required_slots` 的子集元素。"""
    state = _state(required_slots=["a"], slots=_slots(a="filled", extra="missing"))
    assert slots_ready(state) is True


def test_slots_ready_accepts_slot_objects() -> None:
    """state 里的槽位可能是 `SlotValue` 对象（节点内）或 dict（检查点恢复后），两者都要认。"""
    from app.agent.state import SlotValue

    state = _state(
        required_slots=["a"],
        slots={"a": SlotValue(key="a", value="x", status="filled", source="user_answer")},
    )
    assert slots_ready(state) is True
