"""T2-37：`R-DOSAGE` 剂量区间与 `R-MAX-USE` 每季次数。

两条规则同文件，因为都从知识条目读取登记值、共用一个"取条目"前置步骤；但它们回答不同
问题——`R-DOSAGE` 是"量对不对"，`R-MAX-USE` 是"次数超没超"。因此测试分组覆盖，并在同
一处核对两者的用药史窗口口径必须一致（`R-CONFLICT` 复用同一份过滤逻辑）。
"""

from datetime import date
from pathlib import Path

from app.gate.enums import GateRuleId
from app.gate.rules.base import build_rules
from app.gate.rules.dosage import DosageRangeRule, MaxUsePerSeasonRule
from tests.gate.fixtures.factories import gate_context, medication_record, pesticide_item, prescription
from tests.gate.fixtures.fake_knowledge import FakeEntry, FakeKnowledge

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "dosage.py"

TODAY = date(2025, 6, 1)
SEASON_START = date(2025, 5, 1)


def _knowledge(dosage_min=10.0, dosage_max=30.0, max_uses=2) -> FakeKnowledge:
    return FakeKnowledge(
        {
            "K-1001": FakeEntry(
                knowledge_id="K-1001",
                name="吡虫啉",
                dosage_min=dosage_min,
                dosage_max=dosage_max,
                max_uses_per_season=max_uses,
            )
        }
    )


# ---- R-DOSAGE ----


def test_dosage_within_range_passes() -> None:
    """正例：处方剂量恰落在登记区间 [20, 30] 内，返回空列表。"""
    ctx = gate_context(
        knowledge=_knowledge(dosage_min=20.0, dosage_max=30.0),
        prescription=prescription(items=[pesticide_item(dosage_min=20.0, dosage_max=30.0)]),
    )

    assert DosageRangeRule().check(ctx) == []


def test_dosage_absent_passes() -> None:
    """正例：`dosage_min` 与 `dosage_max` 均为 None，"该不该给剂量"由别的规则决定。"""
    ctx = gate_context(
        knowledge=_knowledge(),
        prescription=prescription(items=[pesticide_item(dosage_min=None, dosage_max=None)]),
    )

    assert DosageRangeRule().check(ctx) == []


def test_dosage_above_max_blocked() -> None:
    """反例：`dosage_max=45` 超出登记上限 30。"""
    ctx = gate_context(
        knowledge=_knowledge(dosage_min=20.0, dosage_max=30.0),
        prescription=prescription(items=[pesticide_item(dosage_min=20.0, dosage_max=45.0)]),
    )

    (violation,) = DosageRangeRule().check(ctx)

    assert violation.severity == "block"
    assert violation.field == "pesticide_items[0].dosage_max"
    assert "高于登记上限" in violation.message


def test_dosage_inverted_range_blocked() -> None:
    """反例：`dosage_min=50` > `dosage_max=30`，数值落在区间外且语义翻转。"""
    ctx = gate_context(
        knowledge=_knowledge(dosage_min=20.0, dosage_max=60.0),
        prescription=prescription(items=[pesticide_item(dosage_min=50.0, dosage_max=30.0)]),
    )

    violations = DosageRangeRule().check(ctx)

    assert len(violations) == 1
    assert "上下限颠倒" in violations[0].message


def test_dosage_skips_missing_entry() -> None:
    """条目不存在时本规则不报——该情形由 `R-WHITELIST` 单独负责，不重复报违规。"""
    ctx = gate_context(
        knowledge=FakeKnowledge({}),
        prescription=prescription(items=[pesticide_item(knowledge_id="K-404")]),
    )

    assert DosageRangeRule().check(ctx) == []


# ---- R-MAX-USE ----


def test_max_use_within_declared_limit_passes() -> None:
    """正例：登记上限 3 次，处方声明 2 次，本季已用 0 次。"""
    ctx = gate_context(
        knowledge=_knowledge(max_uses=3),
        prescription=prescription(items=[pesticide_item(max_uses_per_season=2)]),
        medication_history=[],
    )

    assert MaxUsePerSeasonRule().check(ctx) == []


def test_max_use_ignores_records_before_season_start() -> None:
    """正例：5 条用药史全部早于 `season_start`，不计入本季累计。"""
    history = [medication_record(pesticide_name="吡虫啉", used_on=date(2025, 4, day)) for day in (1, 2, 3, 4, 5)]
    ctx = gate_context(
        knowledge=_knowledge(max_uses=2),
        prescription=prescription(items=[pesticide_item(max_uses_per_season=2)]),
        medication_history=history,
        season_start=SEASON_START,
    )

    assert MaxUsePerSeasonRule().check(ctx) == []


def test_max_use_declared_exceeds_limit_blocked() -> None:
    """反例：登记上限 3 次，处方声明 4 次。

    上限来自红线配置 `R-MAX-USE.limit`（TEA-03 分支），配置侧应保证 `limit <= 登记值`。
    """
    ctx = gate_context(
        knowledge=_knowledge(max_uses=3),
        prescription=prescription(items=[pesticide_item(max_uses_per_season=4)]),
        rule_configs={GateRuleId.MAX_USE.value: {"limit": 3}},
    )

    violations = MaxUsePerSeasonRule().check(ctx)

    assert violations
    assert "超出登记上限" in violations[0].message


def test_max_use_history_exhausted_blocked() -> None:
    """反例：登记上限 2 次，本季该条目已用 2 次，再加本次即超限。"""
    history = [
        medication_record(pesticide_name="吡虫啉", used_on=date(2025, 5, 10)),
        medication_record(pesticide_name="吡虫啉", used_on=date(2025, 5, 20)),
    ]
    ctx = gate_context(
        knowledge=_knowledge(max_uses=2),
        prescription=prescription(items=[pesticide_item(max_uses_per_season=2)]),
        medication_history=history,
        season_start=SEASON_START,
    )

    violations = MaxUsePerSeasonRule().check(ctx)

    assert violations
    assert "本季已用 2 次" in violations[0].message
    assert "超过上限" in violations[0].message


def test_max_use_configured_limit_applies() -> None:
    """红线配置的 `limit` 是流程级更严格上限，优先于条目登记值。"""
    ctx = gate_context(
        knowledge=_knowledge(max_uses=5),
        prescription=prescription(items=[pesticide_item(max_uses_per_season=4)]),
        rule_configs={GateRuleId.MAX_USE.value: {"limit": 3}},
    )

    violations = MaxUsePerSeasonRule().check(ctx)

    assert violations
    assert "超出登记上限 3 次" in violations[0].message


# ---- 装配与窗口口径一致 ----


def test_build_both_rules() -> None:
    """按 id 子集实例化返回两个实例，id 不同且同为 block。"""
    rules = build_rules([GateRuleId.DOSAGE.value, GateRuleId.MAX_USE.value])

    assert len(rules) == 2
    assert {r.rule_id for r in rules} == {GateRuleId.DOSAGE.value, GateRuleId.MAX_USE.value}
    assert all(r.severity == "block" for r in rules)
    assert {type(r) for r in rules} == {DosageRangeRule, MaxUsePerSeasonRule}


def test_history_window_boundary_is_inclusive() -> None:
    """恰在 `season_start` 当天的记录纳入累计：窗口用 `>=` 而非 `>`。

    该口径与 `R-CONFLICT` 共用 `app.gate.matching.recent_records`，两处若有一处漂移，
    就会出现"次数按本季算、冲突按全年算"的漏判。
    """
    ctx = gate_context(
        knowledge=_knowledge(max_uses=1),
        prescription=prescription(items=[pesticide_item(max_uses_per_season=1)]),
        medication_history=[medication_record(pesticide_name="吡虫啉", used_on=SEASON_START)],
        season_start=SEASON_START,
    )

    violations = MaxUsePerSeasonRule().check(ctx)

    assert violations
    assert "本季已用 1 次" in violations[0].message


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
    assert "await " not in source
    assert "httpx" not in source
    assert "gateway" not in source
