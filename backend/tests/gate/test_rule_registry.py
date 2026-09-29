"""T2-34：规则注册表的唯一性、白名单与十二条齐备。

注册表是"配置里声明的规则 id"与"代码里实现的规则"之间的桥。它的三种失败模式都必须抛
`GateConfigError` 且不被静默吞掉：`rule_id` 不在 `GateRuleId` 白名单内、同一 id 重复登记、
已声明规则漏实现（`build_rules` 的 missing 分支）。第一条与第三条是配置漂移的来源，第二条
是导入顺序错误（同一模块被 import 两次）的信号。

"重复登记"要先清空再逐个登记来构造：生产注册表在导入期已填满，直接再登记必然因重复而抛错，
但那证明不了"重复检测"本身——必须让第一次登记成功、第二次才失败。注册表是模块级全局，
测试改动后用快照还原，避免污染同进程内后续用例。
"""

import inspect

import pytest

from app.gate.enums import GateRuleId
from app.gate.rules import base as rules_base
from app.gate.rules.base import GateConfigError, Rule, build_rules, register, registered_rule_ids


@pytest.fixture
def isolated_registry():
    """把注册表隔离成空表，用例结束后按快照还原（模块级全局，必须还原）。"""
    snapshot = dict(rules_base._REGISTRY)
    rules_base._clear_registry()
    try:
        yield
    finally:
        rules_base._clear_registry()
        rules_base._REGISTRY.update(snapshot)


def test_all_declared_rule_ids_can_be_built() -> None:
    """十二条 id（八个实现文件）全部可实例化，且实例数与 id 数一致。"""
    rules = build_rules()

    assert len(rules) == len(GateRuleId)
    assert {r.rule_id for r in rules} == {r.value for r in GateRuleId}


def test_registered_rule_ids_match_enum() -> None:
    """注册表恰好覆盖枚举，不多不少。"""
    assert registered_rule_ids() == sorted(r.value for r in GateRuleId)


def test_build_subset_only_returns_requested() -> None:
    """按 id 子集实例化：引擎可只跑某几条规则（测试与影子比对会用到）。"""
    rules = build_rules([GateRuleId.REDLINE_LOAD.value, GateRuleId.WHITELIST.value])

    assert [r.rule_id for r in rules] == [GateRuleId.REDLINE_LOAD.value, GateRuleId.WHITELIST.value]


def test_unknown_rule_id_raises_with_id_in_message() -> None:
    """未登记的 id 直接抛错，消息里带该 id，不静默跳过。"""
    with pytest.raises(GateConfigError, match="R-NOT-EXIST"):
        build_rules(["R-NOT-EXIST"])


def test_register_rejects_rule_id_outside_enum(isolated_registry) -> None:
    """`rule_id` 不在 `GateRuleId` 白名单内：配置里写不存在的规则由这里兜底。"""

    class _StrayRule(Rule):
        rule_id = "R-NOT-IN-ENUM"
        severity = "warn"

        def check(self, ctx):
            return []

    with pytest.raises(GateConfigError, match="不在 GateRuleId 内"):
        register(_StrayRule)


def test_duplicate_registration_raises(isolated_registry) -> None:
    """同一 rule_id 第二次登记必须抛错，而不是悄悄覆盖第一条实现。"""

    class _First(Rule):
        rule_id = GateRuleId.WHITELIST.value
        severity = "block"

        def check(self, ctx):
            return []

    class _Second(Rule):
        rule_id = GateRuleId.WHITELIST.value
        severity = "block"

        def check(self, ctx):
            return []

    register(_First)
    with pytest.raises(GateConfigError, match="重复登记"):
        register(_Second)


def test_abstract_rule_cannot_be_registered(isolated_registry) -> None:
    """抽象规则（还有未实现的 `check`）不得登记，否则实例化会在运行期才炸。"""

    class _StillAbstract(Rule):
        rule_id = GateRuleId.CONFLICT.value
        severity = "block"

    with pytest.raises(GateConfigError, match="抽象方法"):
        register(_StillAbstract)


def test_registry_restored_after_isolation() -> None:
    """保证隔离夹具用完即还原：后续用例仍能构建全部规则。"""
    assert len(registered_rule_ids()) == len(GateRuleId)


@pytest.mark.parametrize("rule_id", [r.value for r in GateRuleId])
def test_every_rule_is_a_pure_sync_checker(rule_id: str) -> None:
    """八条实现文件共同约定：`check` 是同步方法（不得是 `async def`），规则无构造参数。"""
    (rule,) = build_rules([rule_id])
    assert not inspect.iscoroutinefunction(rule.check)
    assert rule.check.__code__.co_argcount == 2  # self + ctx
