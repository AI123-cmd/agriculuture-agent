"""规则抽象基类与规则注册表（T2-34）。

规则是**同构纯函数**：只读 `ctx`，不做 IO、不调模型、不读全局配置。每条规则都能脱离数据库
与模型单独单测，这是技术方案 14.2"闸门即评测关卡"能落地的前提。

`check` 返回违规列表而非 `GateResult`：规则只负责"我发现什么"，"整体怎么裁定"由引擎统一
决定，新增规则无需理解 decision 聚合。`severity` 是类属性而非 `check` 参数，因为等级属于
规则定义，不属于某次判定。
"""

import inspect
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity


class GateConfigError(RuntimeError):
    """规则注册或实例化时的配置错误。启动期暴露，不允许静默跳过。"""


class Rule(ABC):
    """一条闸门规则。实现为纯函数：只读 `ctx`，不做 IO，不调模型。"""

    rule_id: ClassVar[str]
    severity: ClassVar[GateSeverity]
    description: ClassVar[str] = ""

    @abstractmethod
    def check(self, ctx: GateContext) -> list[GateViolation]:
        """返回本轮命中的全部违规。无命中返回空列表，不得返回 None。"""

    def violation(self, field: str, message: str, severity: GateSeverity | None = None) -> GateViolation:
        """按规则自身的 id 与等级构造违规，避免每条规则各写一遍 `rule_id`。"""
        return GateViolation(
            rule_id=self.rule_id,
            severity=severity if severity is not None else self.severity,
            message=message,
            field=field,
        )


# 规则 id → 规则类。以 `GateRuleId` 为白名单：配置里写了不存在的 id 由 T2-06 拦，
# 代码侧漏实现一条已声明规则由 `build_rules` 的 missing 分支发现。
_REGISTRY: dict[str, type[Rule]] = {}


def register(cls: type[Rule]) -> type[Rule]:
    """登记规则类。`rule_id` 必须是枚举值、不得重复、不得是抽象类。"""
    rid = getattr(cls, "rule_id", None)
    if rid not in {r.value for r in GateRuleId}:
        raise GateConfigError(f"{cls.__name__} 的 rule_id {rid!r} 不在 GateRuleId 内")
    if rid in _REGISTRY:
        raise GateConfigError(f"规则 id 重复登记: {rid}")
    if inspect.isabstract(cls):
        raise GateConfigError(f"{cls.__name__} 仍有未实现的抽象方法")
    _REGISTRY[rid] = cls
    return cls


def build_rules(rule_ids: Iterable[str] | None = None) -> list[Rule]:
    """实例化规则。`None` 表示全部；未登记 id 直接抛错，不静默跳过。"""
    ids = [r.value for r in GateRuleId] if rule_ids is None else list(rule_ids)
    missing = [i for i in ids if i not in _REGISTRY]
    if missing:
        raise GateConfigError(f"规则未登记: {missing}")
    return [_REGISTRY[i]() for i in ids]


def registered_rule_ids() -> list[str]:
    """已登记规则 id（升序）。供测试断言十二条齐备。"""
    return sorted(_REGISTRY)


def _clear_registry() -> None:
    """清空注册表。仅供测试构造"重复登记"场景，生产代码不得调用。"""
    _REGISTRY.clear()


__all__ = ["GateConfigError", "Rule", "build_rules", "register", "registered_rule_ids"]
