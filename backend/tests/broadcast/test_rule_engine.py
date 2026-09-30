"""T3-05 验收：规则层匹配引擎（零模型可用）。

覆盖验收标准的五个点：零模型模式（结构性，非开关）、环境不可用时环境风险类被拦截、
五段字段完整性与 `validate_item` 的缺段报错、按流程筛选（`applies_to` 而非 `if` 分支）、
以及引擎会合并农事历/回访两路已构造好的条目。

本文件全部用构造对象，不起数据库、不触模型：这正对应"规则层必须能在零模型模式下工作"
这条核心约束——如果这些用例需要模型或数据库，约束就没被真正满足。
"""

import inspect
from dataclasses import FrozenInstanceError

import pytest

from app.agent.state import ProcessId
from app.broadcast.inputs.env_facts import EnvFacts
from app.broadcast.rules import (
    RuleContext,
    RuleEngine,
    RuleImplementationError,
    RuleResult,
    validate_item,
)
from app.broadcast.rules.base import BaseRule
from app.process_config.loader import load_process_config


class _RiceOnlyRule(BaseRule):
    """只适用于水稻的桩规则：用于验证按流程筛选走 `applies_to` 而不改引擎。"""

    process_ids = frozenset({ProcessId.RICE})

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        return [
            RuleResult(
                source_kind="env_risk",
                severity="notice",
                callout="水稻",
                fact="仅水稻适用的事实",
                impact="仅水稻适用的影响",
                action="仅水稻适用的行动",
                rule_id="test.rice_only",
            )
        ]


def _ctx(**kwargs) -> RuleContext:
    """构造上下文：默认环境不可用且无配置，用例按需覆盖。"""
    params = {"env": EnvFacts(source="none", values={})}
    params.update(kwargs)
    return RuleContext(**params)


def _calendar_item(rule_id: str = "calendar:rice_boot_window") -> RuleResult:
    return RuleResult(
        source_kind="calendar",
        severity="reminder",
        callout="东边水田",
        fact="孕穗期对稻瘟病敏感",
        impact="当前处于孕穗期",
        action="按该时令要点安排农事",
        rule_id=rule_id,
    )


def _followup_item(rule_id: str = "followup.observing.rice") -> RuleResult:
    return RuleResult(
        source_kind="followup",
        severity="reminder",
        callout="水稻·3亩",
        fact="你 3 天前问的叶片斑点",
        impact="当时建议先观察两天",
        action="看看有没有变化",
        rule_id=rule_id,
    )


# ---- 一、零模型模式（结构性） ----


def test_match_all_is_not_a_coroutine() -> None:
    """`match_all` 不是协程：函数体内不存在 `await`，模型调用无法插入。"""
    assert inspect.iscoroutinefunction(RuleEngine.match_all) is False


def test_engine_signatures_have_no_model_gateway() -> None:
    """构造签名与 `match_all` 签名都不含 llm / gateway 参数（结构性保证，非开关）。"""
    forbidden = {"llm", "gateway", "model", "client"}
    assert forbidden.isdisjoint(inspect.signature(RuleEngine.__init__).parameters)
    assert forbidden.isdisjoint(inspect.signature(RuleEngine.match_all).parameters)


def test_match_all_runs_without_any_model() -> None:
    """即使把模型网关整体视为不可用，`match_all` 仍正常返回且不抛异常。"""
    engine = RuleEngine(rules=[_RiceOnlyRule()])
    items = engine.match_all(_ctx(process_id=ProcessId.RICE, env=EnvFacts(source="sensor", values={})))
    assert len(items) == 1


# ---- 二、环境不可用时的拦截 ----


def test_source_none_blocks_env_risk_but_keeps_other_kinds() -> None:
    """`source="none"` 且档案完整：env_risk 数量为 0，calendar / followup 正常产出。"""
    engine = RuleEngine()
    items = engine.match_all(
        _ctx(
            process_id=ProcessId.RICE,
            env=EnvFacts(source="none", values={}),
            calendar=[_calendar_item()],
            signals=[_followup_item()],
        )
    )
    env_risk = [i for i in items if i.source_kind == "env_risk"]
    assert env_risk == []
    assert [i.source_kind for i in items].count("calendar") == 1
    assert [i.source_kind for i in items].count("followup") == 1


def test_env_available_produces_env_risk_on_threshold_hit() -> None:
    """环境可用且越过阈值时，环境风险类条目正常产出。"""
    engine = RuleEngine()
    config = load_process_config(ProcessId.RICE)
    items = engine.match_all(
        _ctx(
            process_id=ProcessId.RICE,
            process_config=config,
            env=EnvFacts(source="sensor", values={"air_temp": 36.0}, provenance={"air_temp": "sensor"}),
        )
    )
    heat = [i for i in items if i.rule_id == "env_risk.heat"]
    assert len(heat) == 1
    assert heat[0].source_kind == "env_risk"


# ---- 三、五段字段完整性与 validate_item ----


def test_env_risk_item_has_five_sections_and_provenance() -> None:
    """高温命中条目含全部五段字段（callout/severity/fact/impact/action）且来源非空。"""
    engine = RuleEngine()
    config = load_process_config(ProcessId.RICE)
    items = engine.match_all(
        _ctx(
            process_id=ProcessId.RICE,
            process_config=config,
            env=EnvFacts(source="sensor", values={"air_temp": 36.0}, provenance={"air_temp": "sensor"}),
        )
    )
    item = next(i for i in items if i.rule_id == "env_risk.heat")
    assert item.callout and item.fact and item.impact and item.action
    assert item.severity in {"urgent", "notice", "reminder"}
    assert item.facts_provenance


def test_validate_item_raises_on_missing_action() -> None:
    """缺 `action` 的条目视为规则实现错误，集中校验点抛 `RuleImplementationError`。"""
    bad = RuleResult(
        source_kind="env_risk",
        severity="notice",
        callout="水稻",
        fact="有事实",
        impact="有影响",
        action="",
        rule_id="test.broken",
    )
    with pytest.raises(RuleImplementationError):
        validate_item(bad)


@pytest.mark.parametrize("field_name", ["callout", "fact", "impact", "action"])
def test_validate_item_raises_for_each_missing_section(field_name: str) -> None:
    """五段中任一空段都报错（含仅空白字符的情形）。"""
    payload = {
        "source_kind": "env_risk",
        "severity": "notice",
        "callout": "水稻",
        "fact": "有事实",
        "impact": "有影响",
        "action": "有行动",
        "rule_id": "test.broken",
    }
    payload[field_name] = "   "
    with pytest.raises(RuleImplementationError):
        validate_item(RuleResult(**payload))


def test_validate_item_returns_item_when_complete() -> None:
    """字段完整时原样返回，供链式调用。"""
    item = _calendar_item()
    assert validate_item(item) is item


# ---- 四、按流程筛选（applies_to，不用 if 分支） ----


def test_rice_only_rule_applies_under_rice() -> None:
    engine = RuleEngine(rules=[_RiceOnlyRule()])
    items = engine.match_all(_ctx(process_id=ProcessId.RICE, env=EnvFacts(source="sensor", values={})))
    assert [i.rule_id for i in items] == ["test.rice_only"]


def test_rice_only_rule_skipped_under_tea() -> None:
    engine = RuleEngine(rules=[_RiceOnlyRule()])
    items = engine.match_all(_ctx(process_id=ProcessId.TEA, env=EnvFacts(source="sensor", values={})))
    assert items == []


def test_rule_result_is_frozen() -> None:
    """条目不可变：合并/去重/排序过程中不会被就地改坏，保证"被抑制"可复现。"""
    item = _calendar_item()
    with pytest.raises(FrozenInstanceError):
        item.fact = "改不动"  # type: ignore[misc]
