"""T2-41：`R-PROVENANCE` 数字溯源。

对展示文本做**反向扫描**（从文本找数字），逐个核对是否在 `numeric_tokens` 中有登记。
与之互补的 `R-NO-DATA`（T2-44）是**正向核对**（从数值字段找漏登记）。本文件覆盖命中、
结构性豁免、单位白名单三类。
"""

from pathlib import Path

import pytest

from app.gate.contracts import GatePayload
from app.gate.enums import GateRuleId
from app.gate.rules.base import build_rules
from app.gate.rules.provenance import UNIT_PATTERN, ProvenanceRule
from tests.gate.fixtures.factories import gate_context

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "provenance.py"
_PAYLOAD_FIELD_READERS = (
    Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "process_redline.py",
    Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "presentation.py",
)


def _run(ctx):
    return ProvenanceRule().check(ctx)


# ---- 正例 ----


def test_registered_token_passes() -> None:
    """正例 1：文本含 30克 且 `numeric_tokens` 已登记该 token。"""
    ctx = gate_context(numeric_tokens={"30克": "pesticide_items[0].dosage_max"})

    assert _run(ctx) == []


def test_structural_step_number_exempted() -> None:
    """正例 2：步骤序号"1."属结构性数字，豁免生效。"""
    ctx = gate_context(payload=GatePayload(rendered_text="1. 清理病株\n2. 观察新叶"))

    assert _run(ctx) == []


# ---- 反例 ----


def test_unregistered_token_blocked() -> None:
    """反例 1：文本含 50克 而登记表为空。"""
    ctx = gate_context(payload=GatePayload(rendered_text="每亩用50克喷雾"), numeric_tokens={})

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "facts_provenance"
    assert "50克" in violation.message
    assert "无事实来源登记" in violation.message


def test_partially_registered_text_blocked() -> None:
    """反例 2：只登记了 30克，文本里的 7天 未登记。"""
    ctx = gate_context(
        payload=GatePayload(rendered_text="每亩用30克，安全间隔期7天"),
        numeric_tokens={"30克": "pesticide_items[0].dosage_max"},
    )

    (violation,) = _run(ctx)

    assert violation.field == "facts_provenance"
    assert "7天" in violation.message


# ---- 单位白名单 ----


@pytest.mark.parametrize(
    ("unit", "sample"),
    [
        ("亩", "5亩"),
        ("克", "50克"),
        ("毫升", "20毫升"),
        ("千克", "5千克"),
        ("公斤", "5公斤"),
        ("升", "3升"),
        ("%", "10%"),
        ("℃", "30℃"),
        ("天", "7天"),
        ("小时", "2小时"),
        ("次", "3次"),
        ("头", "2头"),
        ("株", "10株"),
        ("丛", "6丛"),
        ("片", "4片"),
    ],
)
def test_unit_pattern_matches_all_whitelisted_units(unit: str, sample: str) -> None:
    """15 个单位各给一个样例，断言正则匹配到单位本身。"""
    match = UNIT_PATTERN.search(sample)

    assert match is not None
    assert match.group(2) == unit


# ---- 装配与文本口径 ----


def test_rule_metadata_and_registration() -> None:
    assert ProvenanceRule.rule_id == GateRuleId.PROVENANCE.value
    assert ProvenanceRule.severity == "block"

    (rule,) = build_rules([GateRuleId.PROVENANCE.value])

    assert isinstance(rule, ProvenanceRule)


def test_payload_field_shared_by_three_rules() -> None:
    """`R-PROVENANCE`/`R-REDLINE`/`R-CAUTION` 读的都是 `GatePayload.rendered_text` 同一字段。"""
    assert "rendered_text" in _RULE_MODULE.read_text(encoding="utf-8")
    for reader in _PAYLOAD_FIELD_READERS:
        assert "rendered_text" in reader.read_text(encoding="utf-8")


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
