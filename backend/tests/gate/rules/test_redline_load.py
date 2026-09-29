"""T2-43：`R-REDLINE-LOAD` 红线配置加载校验。

"红线缺失即不可用"在闸门内的落点。整条规则只有两个分支，这是刻意的：它必须简单到无法
出错，因为它决定的是"其余校验结果是否可信"。本文件覆盖加载成功/失败两态与消息特征。
"""

from pathlib import Path

from app.gate.contracts import GateContext
from app.gate.enums import GateRuleId, ProcessId
from app.gate.rules.base import build_rules
from app.gate.rules.redline_load import RedlineLoadRule
from app.process_config.redlines import LoadedRedlines
from tests.gate.fixtures.factories import gate_context
from tests.gate.fixtures.fake_knowledge import loaded_redlines, unavailable_redlines

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "redline_load.py"


def _run(ctx):
    return RedlineLoadRule().check(ctx)


# ---- 正例 ----


def test_loaded_with_redlines_passes() -> None:
    """正例 1：`loaded=True` 且 `redlines` 非空，返回空列表。"""
    ctx = gate_context(redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"]))

    assert _run(ctx) == []


def test_loaded_but_empty_redlines_passes() -> None:
    """正例 2：`loaded=True` 但 `redlines` 为空元组——判据只看 `loaded`，不看列表长度。"""
    empty_but_loaded = LoadedRedlines(process_id=ProcessId.TEA, version=1, redlines=(), loaded=True)
    ctx = gate_context(redlines=empty_but_loaded)

    assert _run(ctx) == []


# ---- 反例 ----


def test_unloaded_with_reason_blocked() -> None:
    """反例 1：加载失败，`field` 指向加载标志，message 带流程 id 与原因。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        redlines=unavailable_redlines(ProcessId.TEA, reason="redlines 为空"),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "process_redlines_loaded"
    assert "tea" in violation.message
    assert "redlines 为空" in violation.message


def test_unloaded_without_reason_reports_fallback_text() -> None:
    """反例 2：无 `unavailable_reason` 时补默认原因文本，不让消息出现空白。"""
    no_reason = LoadedRedlines(process_id=ProcessId.RICE, version=1, redlines=(), loaded=False, unavailable_reason=None)
    ctx = gate_context(redlines=no_reason)

    (violation,) = _run(ctx)

    assert "未给出原因" in violation.message


# ---- 装配与排序 ----


def test_rule_metadata_and_registration() -> None:
    assert RedlineLoadRule.rule_id == GateRuleId.REDLINE_LOAD.value
    assert RedlineLoadRule.severity == "block"

    (rule,) = build_rules([GateRuleId.REDLINE_LOAD.value])

    assert isinstance(rule, RedlineLoadRule)


def test_precheck_rule_sorts_first() -> None:
    """引擎把本规则排到执行顺序第一位（`_PRECHECK_RULE_IDS` 生效）。"""
    from app.gate.engine import GateEngine

    engine = GateEngine(rules=build_rules())

    assert engine._rules[0].rule_id == GateRuleId.REDLINE_LOAD.value


def test_context_has_no_generic_fallback_switch() -> None:
    """`GateContext` 不存在任何"降级为通用规则"的开关字段。"""
    for forbidden in ("fallback_rules", "allow_generic", "use_generic_rules"):
        assert forbidden not in GateContext.model_fields


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
