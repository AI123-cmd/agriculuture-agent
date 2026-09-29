"""T2-38：`R-INTERVAL` 安全间隔期。

三条硬约束：间隔期必须显式给出；茶流程必须与下一轮采摘计划核算不冲突；茶流程缺
`next_pick_date` 即 block（fail-closed）。`days_to_pick` 允许为负——农户填了一个已过去的
采摘日时方案本身需要人工确认，不能自动放行。
"""

from datetime import date, timedelta
from pathlib import Path

from app.gate.enums import GateRuleId, ProcessId
from app.gate.rules.base import build_rules
from app.gate.rules.safety_interval import SafetyIntervalRule
from tests.gate.fixtures.factories import gate_context, pesticide_item, prescription

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "safety_interval.py"

TODAY = date(2025, 6, 1)


def _run(ctx):
    return SafetyIntervalRule().check(ctx)


# ---- 正例 ----


def test_tea_interval_satisfied_passes() -> None:
    """正例 1（茶）：间隔期 7 天，下一轮采摘在 10 天后，不冲突。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        slots={"next_pick_date": TODAY + timedelta(days=10)},
    )

    assert _run(ctx) == []


def test_generic_no_pesticide_passes() -> None:
    """正例 2（通用）：无药剂，间隔期 None 也不触发分支 1（没有用药就没有间隔期问题）。"""
    ctx = gate_context(
        process_id=ProcessId.GENERAL,
        prescription=prescription(items=[], safety_interval_days=None),
        today=TODAY,
    )

    assert _run(ctx) == []


# ---- 反例 ----


def test_missing_interval_blocked() -> None:
    """反例 1：涉及用药但顶层未给出安全间隔期。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=None),
        today=TODAY,
        slots={"next_pick_date": TODAY + timedelta(days=10)},
    )

    violations = _run(ctx)

    assert len(violations) == 1
    assert violations[0].field == "safety_interval_days"
    assert "未给出安全间隔期" in violations[0].message


def test_tea_missing_next_pick_blocked() -> None:
    """反例 2（茶）：`next_pick_date` 为 `"unknown"`，视为缺失，走 fail-closed 分支。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        slots={"next_pick_date": "unknown"},
    )

    (violation,) = _run(ctx)

    assert violation.field == "slots.next_pick_date"
    assert "缺少下一轮采摘计划" in violation.message


def test_tea_pick_too_soon_blocked() -> None:
    """反例 3（茶）：距采摘仅 3 天，短于 7 天间隔期。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        slots={"next_pick_date": TODAY + timedelta(days=3)},
    )

    (violation,) = _run(ctx)

    assert "距下一轮采摘仅 3 天" in violation.message


def test_tea_past_pick_date_still_blocked() -> None:
    """反例 4（茶）：采摘日已过去，`days_to_pick` 为负，仍小于间隔期而触发。"""
    past = TODAY - timedelta(days=1)
    ctx = gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        slots={"next_pick_date": past},
    )

    (violation,) = _run(ctx)

    assert (past - TODAY).days < 0
    assert "距下一轮采摘仅 -1 天" in violation.message


def test_non_iso_slot_treated_as_missing() -> None:
    """槽位是非 ISO 字符串时 `slot_date` 返回 None，走"缺少采摘计划"，这是 fail-closed。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        slots={"next_pick_date": "下个月吧"},
    )

    assert ctx.slot_date("next_pick_date") is None

    (violation,) = _run(ctx)

    assert violation.field == "slots.next_pick_date"
    assert "缺少下一轮采摘计划" in violation.message


def test_configured_floor_blocked() -> None:
    """配置下限分支：`min_days=10` 而处方仅 7 天，对全部流程生效。"""
    ctx = gate_context(
        process_id=ProcessId.GENERAL,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        rule_configs={GateRuleId.INTERVAL.value: {"min_days": 10}},
    )

    (violation,) = _run(ctx)

    assert violation.field == "safety_interval_days"
    assert "短于本流程配置下限 10 天" in violation.message


# ---- 装配 ----


def test_rule_metadata_and_registration() -> None:
    assert SafetyIntervalRule.rule_id == GateRuleId.INTERVAL.value
    assert SafetyIntervalRule.severity == "block"

    (rule,) = build_rules([GateRuleId.INTERVAL.value])

    assert isinstance(rule, SafetyIntervalRule)


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
