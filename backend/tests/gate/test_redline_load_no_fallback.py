"""T2-43：红线加载失败时不得降级为通用规则（回归守卫）。

"降级为通用规则继续输出"看起来更服务用户，但在本产品里是绝对错误的：红线的差异正是产品
安全的基础，且缺失原因不可知。本文件用计数包装器断言：`loaded=False` 时除前置规则外，
**任何**通用规则都没有被执行（调用次数为 0），而不是"执行后返回空列表"。
"""

from functools import wraps

import pytest

from app.gate.engine import GateEngine
from app.gate.enums import GateRuleId, ProcessId
from app.gate.rules.base import build_rules
from tests.gate.fixtures.factories import gate_context
from tests.gate.fixtures.fake_knowledge import unavailable_redlines

_GENERIC_RULE_IDS = tuple(rid.value for rid in GateRuleId if rid is not GateRuleId.REDLINE_LOAD)


@pytest.fixture
def counted_engine():
    """构造真实规则实例并包装 `check` 计数，返回 (engine, calls)。"""
    calls: dict[str, int] = {}

    rules = build_rules()
    for rule in rules:
        calls[rule.rule_id] = 0
        original = rule.check

        def make_wrapper(rid: str, func):
            @wraps(func)
            def wrapper(ctx):
                calls[rid] += 1
                return func(ctx)

            return wrapper

        rule.check = make_wrapper(rule.rule_id, original)  # type: ignore[method-assign]

    return GateEngine(rules=rules), calls


def test_unloaded_redlines_block_and_skip_all_generic_rules(counted_engine) -> None:
    """加载失败：只有前置规则执行，其余规则调用次数均为 0。"""
    engine, calls = counted_engine
    ctx = gate_context(process_id=ProcessId.TEA, redlines=unavailable_redlines(ProcessId.TEA))

    result = engine.evaluate(ctx)

    assert result.checked_rules == [GateRuleId.REDLINE_LOAD.value]
    assert result.decision == "block"
    assert result.passed is False
    assert result.process_redlines_loaded is False

    assert calls[GateRuleId.REDLINE_LOAD.value] == 1
    for rid in _GENERIC_RULE_IDS:
        assert calls[rid] == 0, f"红线未加载时不应执行 {rid}"


def test_uncovered_ids_empty_when_short_circuited() -> None:
    """短路造成的执行缺口不算"漏配"，`uncovered_rule_ids` 返回空列表。"""
    engine = GateEngine()
    ctx = gate_context(process_id=ProcessId.TEA, redlines=unavailable_redlines(ProcessId.TEA))

    result = engine.evaluate(ctx)

    assert engine.uncovered_rule_ids(ctx, result) == []


def test_uncovered_ids_reports_removed_declared_rule() -> None:
    """`loaded=True` 且人为移除一个声明规则时，`uncovered_rule_ids` 返回该 id。"""
    from tests.gate.fixtures.fake_knowledge import loaded_redlines

    engine = GateEngine()
    ctx = gate_context(process_id=ProcessId.TEA, redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"]))

    result = engine.evaluate(ctx)
    assert GateRuleId.REDLINE.value in result.checked_rules

    trimmed = result.model_copy(update={"checked_rules": [r for r in result.checked_rules if r != GateRuleId.REDLINE.value]})

    assert engine.uncovered_rule_ids(ctx, trimmed) == [GateRuleId.REDLINE.value]
