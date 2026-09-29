"""T2-39：`R-CONFLICT` 用药史冲突。

与 `R-MAX-USE` 读同一份用药史但回答不同问题——次数超限是"量"，重复与禁忌是"质"。重复
用药以归一后的药剂名为键（档案记录未必有本系统的 `knowledge_id`），禁忌搭配只查"条目的
禁忌清单是否被近期用药命中"这一个方向（反向数据往往缺失）。
"""

from datetime import date
from pathlib import Path

from app.gate.enums import GateRuleId
from app.gate.rules.base import build_rules
from app.gate.rules.medication_conflict import MedicationConflictRule
from app.gate.rules.dosage import MaxUsePerSeasonRule
from tests.gate.fixtures.factories import gate_context, medication_record, pesticide_item, prescription
from tests.gate.fixtures.fake_knowledge import FakeEntry, FakeKnowledge

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "medication_conflict.py"

TODAY = date(2025, 6, 1)
SEASON_START = date(2025, 5, 1)


def _run(ctx):
    return MedicationConflictRule().check(ctx)


# ---- 正例 ----


def test_empty_history_passes() -> None:
    """正例 1：首次用药，用药史为空，不产生任何违规。"""
    ctx = gate_context(
        medication_history=[],
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(name="吡虫啉")]),
    )

    assert _run(ctx) == []


def test_records_before_season_start_passed() -> None:
    """正例 2：用药史全部早于 `season_start`，不在近期窗口内。"""
    history = [
        medication_record(pesticide_name="吡虫啉", used_on=date(2025, 3, 1)),
        medication_record(pesticide_name="吡虫啉", used_on=date(2025, 4, 1)),
    ]
    ctx = gate_context(
        medication_history=history,
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(name="吡虫啉")]),
    )

    assert _run(ctx) == []


# ---- 反例 ----


def test_duplicate_medication_blocked() -> None:
    """反例 1：本季内用过同名药剂，message 带出重复用药的日期。"""
    history = [medication_record(pesticide_name="吡虫啉", used_on=date(2025, 5, 20))]
    ctx = gate_context(
        medication_history=history,
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(name="吡虫啉")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "pesticide_items[0]"
    assert "用药重复" in violation.message
    assert "2025-05-20" in violation.message


def test_forbidden_combination_matched_after_normalization() -> None:
    """反例 2：条目禁忌清单含"代森锰锌"，档案写作"代森锰锌（可湿性粉剂）"，归一中命中。"""
    knowledge = FakeKnowledge(
        {
            "K-1001": FakeEntry(
                knowledge_id="K-1001",
                name="吡虫啉",
                forbidden_combinations=("代森锰锌",),
            )
        }
    )
    history = [medication_record(pesticide_name="代森锰锌（可湿性粉剂）", used_on=date(2025, 5, 20))]
    ctx = gate_context(
        knowledge=knowledge,
        medication_history=history,
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "pesticide_items[0].forbidden_combinations"
    assert "已知禁忌搭配" in violation.message
    assert "代森锰锌" in violation.message


# ---- 与 R-MAX-USE 的窗口口径一致 ----


def test_window_boundary_shared_with_max_use() -> None:
    """恰在 `season_start` 当天的记录，两条规则都纳入：窗口口径来自同一函数。

    `MaxUsePerSeasonRule._history_used` 调用 `app.gate.matching.recent_records`，
    `MedicationConflictRule` 亦调用同一函数。若有一处漂移，会出现"次数按本季算、
    冲突按全年算"的漏判，因此这里同时驱动两条规则核对边界一致。
    """
    boundary = SEASON_START
    history = [medication_record(pesticide_name="吡虫啉", used_on=boundary)]

    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", max_uses_per_season=1)})
    ctx = gate_context(
        knowledge=knowledge,
        medication_history=history,
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(name="吡虫啉", max_uses_per_season=1)]),
    )

    assert MaxUsePerSeasonRule().check(ctx) != []
    assert MedicationConflictRule().check(ctx) != []


# ---- 装配 ----


def test_rule_metadata_and_registration() -> None:
    assert MedicationConflictRule.rule_id == GateRuleId.CONFLICT.value
    assert MedicationConflictRule.severity == "block"

    (rule,) = build_rules([GateRuleId.CONFLICT.value])

    assert isinstance(rule, MedicationConflictRule)


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
