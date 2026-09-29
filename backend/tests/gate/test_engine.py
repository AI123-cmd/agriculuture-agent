"""T2-34：`GateEngine` 的裁定聚合、fail-closed 与红线上游短路。

引擎是闸门唯一的裁定入口，它的正确性不在"规则判得对不对"（各规则另有独立测试），而在
**聚合语义**：block 是否压过 degrade、任何异常是否一律导向不放行、红线缺失时是否整体短路。
因此这里用最小规则替身直接驱动引擎，把注意力集中在 `_decide` / `_gate_open` / `_run` 三条
内部约定上；红线上游短路那一条则用真实的 `R-REDLINE-LOAD`（替换它没有意义，它本身就是被测点）。
"""

from typing import ClassVar

import pytest
from pydantic import ValidationError

from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.contracts import GateContext, GateViolation
from app.gate.engine import GateEngine
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule
from app.gate.rules.redline_load import RedlineLoadRule
from tests.gate.conftest import gate_context, unavailable_redlines


class _AllowRule(Rule):
    rule_id: ClassVar[str] = GateRuleId.REGION.value
    severity: ClassVar[GateSeverity] = "warn"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        return []


class _DegradeRule(Rule):
    rule_id: ClassVar[str] = GateRuleId.CAUTION.value
    severity: ClassVar[GateSeverity] = "degrade"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        return [self.violation(field="payload.rendered_text", message="出现强制命令语气")]


class _BlockRule(Rule):
    rule_id: ClassVar[str] = GateRuleId.WHITELIST.value
    severity: ClassVar[GateSeverity] = "block"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        return [self.violation(field="pesticide_items", message="药剂不在已核准库")]


class _BoomRule(Rule):
    rule_id: ClassVar[str] = GateRuleId.DOSAGE.value
    severity: ClassVar[GateSeverity] = "block"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        raise RuntimeError("上游事实源字段缺失")


# ---- 裁定聚合 ----


def test_all_clear_allows() -> None:
    """无任何违规时放行，且 `passed` 与 `decision` 一致。"""
    result = GateEngine(rules=[_AllowRule()]).evaluate(gate_context())

    assert result.decision == "allow"
    assert result.passed is True
    assert result.violations == []
    assert result.checked_rules == [GateRuleId.REGION.value]
    assert result.degrade_output is None


def test_single_degrade_violation_degrades_with_output() -> None:
    """一条 degrade 违规即整体降级，且必须带上统一文案（否则等于没有输出）。"""
    result = GateEngine(rules=[_DegradeRule()]).evaluate(gate_context())

    assert result.decision == "degrade"
    assert result.passed is False
    assert result.degrade_output == DEGRADE_OUTPUT_TEXT
    assert [v.severity for v in result.violations] == ["degrade"]


def test_block_wins_over_degrade() -> None:
    """block 与 degrade 混合时取 block：严重度最高的违规决定整体裁定。"""
    engine = GateEngine(rules=[_DegradeRule(), _BlockRule()])
    result = engine.evaluate(gate_context())

    assert result.decision == "block"
    assert result.passed is False
    assert {v.severity for v in result.violations} == {"block", "degrade"}
    assert result.degrade_output is None  # block 不输出降级文案


def test_warn_violation_still_allows() -> None:
    """warn 只留痕不改裁定（8.2 的 warn→allow 但留痕）。"""

    class _WarnRule(Rule):
        rule_id: ClassVar[str] = GateRuleId.NO_DATA.value
        severity: ClassVar[GateSeverity] = "warn"

        def check(self, ctx: GateContext) -> list[GateViolation]:
            return [self.violation(field="facts_provenance", message="一条数值未溯源")]

    result = GateEngine(rules=[_WarnRule()]).evaluate(gate_context())

    assert result.decision == "allow"
    assert result.passed is True
    assert result.violations[0].severity == "warn"


# ---- fail-closed ----


def test_rule_exception_fails_closed() -> None:
    """规则抛异常时引擎仍返回结果，且按最保守方向判 block，message 标明拦截原因。"""
    result = GateEngine(rules=[_BoomRule()]).evaluate(gate_context())

    assert result.decision == "block"
    assert result.passed is False
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation.severity == "block"
    assert "按最保守判定拦截" in violation.message
    assert violation.rule_id == GateRuleId.DOSAGE.value


def test_exception_records_block_then_short_circuits() -> None:
    """异常规则记一条 block 后，后续规则被 `_gate_open` 短路，不再执行也不再留痕。

    引擎按 (前置优先, rule_id) 排序，故 C.. < D.. < R.. 的推进顺序是 CAUTION → DOSAGE(异常)
    → REGION；DOSAGE 抛出后已出现 block，REGION 被跳过，这正是"校验半途而废看起来像通过"
    的反面：缺口体现在 `checked_rules` 里。
    """
    engine = GateEngine(rules=[_AllowRule(), _BoomRule(), _DegradeRule()])
    result = engine.evaluate(gate_context())

    assert result.decision == "block"
    assert result.checked_rules == [GateRuleId.CAUTION.value, GateRuleId.DOSAGE.value]


def test_exception_does_not_abort_engine() -> None:
    """异常不向上传播：引擎照常返回一个可审计的 `GateResult`。"""
    result = GateEngine(rules=[_BoomRule()]).evaluate(gate_context())

    assert result.checked_rules == [GateRuleId.DOSAGE.value]


# ---- 红线上游短路 ----


def test_unloaded_redlines_block_and_short_circuit() -> None:
    """红线未加载：只有前置规则执行，结果 block 且不可用，其余规则整体跳过。"""
    ctx = gate_context(redlines=unavailable_redlines())

    result = GateEngine().evaluate(ctx)

    assert result.passed is False
    assert result.decision == "block"
    assert result.process_redlines_loaded is False
    assert result.checked_rules == [GateRuleId.REDLINE_LOAD.value]


def test_uncovered_rule_ids_empty_when_redlines_missing() -> None:
    """红线未加载导致的执行缺口不算"漏配"，不返回任何未覆盖 id。"""
    engine = GateEngine()
    ctx = gate_context(redlines=unavailable_redlines())

    result = engine.evaluate(ctx)

    assert engine.uncovered_rule_ids(ctx, result) == []


def test_precheck_rule_sorts_first() -> None:
    """前置规则被排序到首位执行，其余规则按 id 稳定排序。"""
    engine = GateEngine(rules=[_BlockRule(), RedlineLoadRule(), _AllowRule()])
    ids = [r.rule_id for r in engine._rules]

    assert ids[0] == GateRuleId.REDLINE_LOAD.value
    assert ids[1:] == sorted([GateRuleId.WHITELIST.value, GateRuleId.REGION.value])


# ---- 物理隔离：闸门拿不到会话文本 ----


def test_context_rejects_conversation_messages() -> None:
    """`extra="forbid"` 让"顺手塞一条会话消息"在构造时报错（8.1 物理隔离）。"""
    ctx = gate_context()
    with pytest.raises(ValidationError):
        GateContext(
            session_id="s",
            user_id="u",
            process_id=ctx.process_id,
            config_version=1,
            prescription=ctx.prescription,
            redlines=ctx.redlines,
            knowledge=ctx.knowledge,
            today=ctx.today,
            messages=[{"role": "user", "content": "x"}],
        )


def test_context_model_fields_have_no_conversation_room() -> None:
    """闸门输入结构里不存在承载会话的字段，隔离是结构性的而非约定性的。"""
    for forbidden in ("messages", "conversation", "history"):
        assert forbidden not in GateContext.model_fields
