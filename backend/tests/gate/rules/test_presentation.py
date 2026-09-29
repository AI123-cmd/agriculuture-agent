"""T2-44：`R-CAUTION` 表述语气（warn）与 `R-NO-DATA` 无数据支撑（block）。

两条规则都读同一份展示文本与溯源表，是"表述层"的两道检查。`R-CAUTION` 是 8.2 中唯一以
warn 为等级的规则：语气问题不致命，但会改变农户的风险感知；它的命中不改变 decision。
`R-NO-DATA` 是正向核对（从数值字段找漏登记），与 `R-PROVENANCE` 的反向扫描互补。
"""

from pathlib import Path

from app.gate.constants import IMPERATIVE_PATTERNS
from app.gate.contracts import GatePayload
from app.gate.engine import GateEngine
from app.gate.enums import GateRuleId
from app.gate.rules.base import build_rules
from app.gate.rules.presentation import CautionToneRule, NoDataSupportRule
from app.gate.rules.provenance import STRUCTURAL_EXEMPTIONS, UNIT_PATTERN
from tests.gate.fixtures.factories import gate_context, pesticide_item, prescription

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "presentation.py"


# ---- R-CAUTION 正例 ----


def test_soft_advice_passes() -> None:
    """正例：含"建议…"建议性措辞且无强制语气。"""
    ctx = gate_context(payload=GatePayload(rendered_text="建议在晴天下午喷施。"))

    assert CautionToneRule().check(ctx) == []


def test_conditional_advice_passes() -> None:
    """正例：含"如果…可以…"条件式建议。"""
    ctx = gate_context(payload=GatePayload(rendered_text="如果连续阴雨，可以延后施药。"))

    assert CautionToneRule().check(ctx) == []


# ---- R-CAUTION 反例 ----


def test_imperative_tone_warned() -> None:
    """反例：命中强制命令语气模式，仅一条 warn。"""
    ctx = gate_context(payload=GatePayload(rendered_text="发现虫害建议立即大量喷洒药剂。"))

    (violation,) = CautionToneRule().check(ctx)

    assert violation.severity == "warn"
    assert violation.field == "gate_payload.rendered_text"
    assert "强制命令语气" in violation.message


def test_missing_softener_warned() -> None:
    """反例：无强制词但整段无建议性措辞（断言式表述），同样留痕。"""
    ctx = gate_context(payload=GatePayload(rendered_text="三天后采摘。"))

    (violation,) = CautionToneRule().check(ctx)

    assert violation.severity == "warn"
    assert "建议性措辞" in violation.message


# ---- R-NO-DATA 正例 ----


def test_no_numbers_passes() -> None:
    """正例：无药剂且无顶层数值字段，`numeric_paths()` 为空。"""
    ctx = gate_context(prescription=prescription(items=[], safety_interval_days=None))

    assert NoDataSupportRule().check(ctx) == []


def test_registered_interval_passes() -> None:
    """正例：`safety_interval_days=7` 且该路径已在 `facts_provenance` 中登记。"""
    ctx = gate_context(
        prescription=prescription(
            items=[pesticide_item(dosage_min=None, dosage_max=None, safety_interval_days=None, max_uses_per_season=None)],
            safety_interval_days=7,
            facts_provenance={"safety_interval_days": "knowledge:K-1001"},
        ),
    )

    assert NoDataSupportRule().check(ctx) == []


# ---- R-NO-DATA 反例 ----


def test_unregistered_interval_blocked() -> None:
    """反例：`safety_interval_days=7` 但溯源表为空。"""
    ctx = gate_context(
        prescription=prescription(
            items=[pesticide_item(dosage_min=None, dosage_max=None, safety_interval_days=None, max_uses_per_season=None)],
            safety_interval_days=7,
            facts_provenance={},
        ),
    )

    violations = NoDataSupportRule().check(ctx)

    assert any(v.field == "safety_interval_days" for v in violations)
    assert all(v.severity == "block" for v in violations)


def test_unregistered_item_dosage_blocked() -> None:
    """反例：药剂条目的 `dosage_min` 有值但未登记，字段路径含索引。"""
    ctx = gate_context(
        prescription=prescription(
            items=[pesticide_item(dosage_min=20.0, dosage_max=None, safety_interval_days=None, max_uses_per_season=None)],
            safety_interval_days=None,
            facts_provenance={},
        ),
    )

    violations = NoDataSupportRule().check(ctx)

    assert any("pesticide_items[0].dosage_min" in v.field for v in violations)


# ---- 等级与 decision 影响 ----


def test_caution_warn_does_not_change_decision() -> None:
    """仅有 warn 违规时 decision 仍为 allow、passed 为 True，但 violations 非空（留痕）。"""
    ctx = gate_context(payload=GatePayload(rendered_text="立即大量喷洒药剂。"))

    assert CautionToneRule.severity == "warn"

    result = GateEngine(rules=[CautionToneRule()]).evaluate(ctx)

    assert result.decision == "allow"
    assert result.passed is True
    assert result.violations != []


def test_no_data_severity_is_block() -> None:
    assert NoDataSupportRule.severity == "block"


# ---- 常量共用与 IMPERATIVE_PATTERNS ----


def test_shares_constants_with_provenance_rule() -> None:
    """`R-NO-DATA` 与 `R-PROVENANCE` 导入的是同一对常量对象（`is` 断言）。"""
    from app.gate.rules import provenance as provenance_module

    assert provenance_module.UNIT_PATTERN is UNIT_PATTERN
    assert provenance_module.STRUCTURAL_EXEMPTIONS is STRUCTURAL_EXEMPTIONS


def test_imperative_patterns_exclude_mandatory_safety_phrasing() -> None:
    """`IMPERATIVE_PATTERNS` 不收录"必须"，因此"必须等待 7 天"是合法表述。"""
    assert not any("必须" in pattern.pattern for pattern in IMPERATIVE_PATTERNS)

    ctx = gate_context(payload=GatePayload(rendered_text="建议施药后必须等待 7 天再采摘。"))

    assert CautionToneRule().check(ctx) == []


def test_build_both_rules() -> None:
    rules = build_rules([GateRuleId.CAUTION.value, GateRuleId.NO_DATA.value])

    assert len(rules) == 2
    assert {r.rule_id for r in rules} == {GateRuleId.CAUTION.value, GateRuleId.NO_DATA.value}


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
