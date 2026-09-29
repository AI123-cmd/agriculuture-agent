"""T2-40：`R-REGION` 地区适宜性。

8.2 中唯一以 degrade 为默认等级的规则：地区不匹配的正确处置是把输出整体替换为转人工
引导文案，而不是拒绝服务。三条分支各自独立——条目的 `region_scope`、配置声明的
`regions`、配置声明的出口 `standards`。本文件同时锁定区划前缀匹配的三种情形。
"""

from pathlib import Path

from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.engine import GateEngine
from app.gate.enums import GateRuleId
from app.gate.rules.base import build_rules
from app.gate.rules.region_fitness import RegionFitnessRule, _matches_prefix
from tests.gate.fixtures.factories import gate_context, pesticide_item, prescription
from tests.gate.fixtures.fake_knowledge import FakeEntry, FakeKnowledge

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "region_fitness.py"


def _run(ctx):
    return RegionFitnessRule().check(ctx)


# ---- 正例 ----


def test_province_scope_covers_city() -> None:
    """正例 1：条目范围 330000 覆盖农户 330100（省覆盖市）。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", region_scope="330000")}
    )
    ctx = gate_context(
        region_code="330100",
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001")]),
    )

    assert _run(ctx) == []


def test_no_scope_and_no_config_passes() -> None:
    """正例 2：条目无地区限定（视为全国通用）且配置未声明 regions/standards。"""
    ctx = gate_context(region_code="330100")

    assert _run(ctx) == []


# ---- 反例 ----


def test_scope_mismatch_degrades() -> None:
    """反例 1：条目范围 320000 不覆盖农户 330100。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", region_scope="320000")}
    )
    ctx = gate_context(
        region_code="330100",
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "degrade"
    assert violation.field == "pesticide_items[K-1001].region_scope"
    assert "不覆盖你所在的 330100" in violation.message


def test_declared_regions_mismatch_degrades() -> None:
    """反例 2：红线配置声明适用地区 320000，农户 330100 不在其中。"""
    ctx = gate_context(
        region_code="330100",
        rule_configs={GateRuleId.REGION.value: {"regions": ["320000"]}},
    )

    (violation,) = _run(ctx)

    assert violation.severity == "degrade"
    assert violation.field == "region_code"
    assert "适用地区" in violation.message


def test_export_standard_not_referenced_degrades() -> None:
    """反例 3：配置声明出口标准 EU MRL 而处方溯源未引用。"""
    ctx = gate_context(
        region_code="330100",
        rule_configs={GateRuleId.REGION.value: {"standards": ["EU MRL"]}},
        prescription=prescription(facts_provenance={"safety_interval_days": "knowledge:K-1001"}),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "degrade"
    assert "EU MRL" in violation.message


# ---- 等级与引擎裁定 ----


def test_severity_is_degrade_and_engine_degrades() -> None:
    """仅此违规时引擎 decision=degrade、passed=False，且 degrade_output 为标准文案。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", region_scope="320000")}
    )
    ctx = gate_context(
        region_code="330100",
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001")]),
    )

    assert RegionFitnessRule.severity == "degrade"

    result = GateEngine(rules=[RegionFitnessRule()]).evaluate(ctx)

    assert result.decision == "degrade"
    assert result.passed is False
    assert result.degrade_output == DEGRADE_OUTPUT_TEXT
    # degrade 是整体替换，不是修改后输出：文案中不含任何被降级的原始处方文本。
    assert "吡虫啉" not in result.degrade_output


# ---- 前缀匹配 ----


def test_matches_prefix_semantics() -> None:
    """上级编码覆盖下级；无法比对（任一侧非数字）时不报。"""
    assert _matches_prefix("330100", "330000") is True
    assert _matches_prefix("330100", "3301") is True
    assert _matches_prefix("330100", "320000") is False


def test_no_region_code_disables_all_branches() -> None:
    """`region_code=None` 时三条分支均不触发。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", region_scope="320000")}
    )
    ctx = gate_context(
        region_code=None,
        knowledge=knowledge,
        rule_configs={GateRuleId.REGION.value: {"regions": ["320000"], "standards": ["EU MRL"]}},
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001")]),
    )

    assert _run(ctx) == []


# ---- 装配 ----


def test_rule_metadata_and_registration() -> None:
    assert RegionFitnessRule.rule_id == GateRuleId.REGION.value

    (rule,) = build_rules([GateRuleId.REGION.value])

    assert isinstance(rule, RegionFitnessRule)


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
