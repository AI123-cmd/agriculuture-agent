"""播报规则引擎基类与注册表（沿用 ai_router 的装饰器注册模式）。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.broadcast.snapshot import EnvSnapshot
    from app.core.config import Settings


@dataclass
class RuleResult:
    """单条播报提醒的结构化结果。"""

    type: str
    level: str          # danger / warning / normal
    title: str
    content: str
    source: str         # sensor / weather / calendar / ai
    action: str = ""
    meta: dict | None = None


class BaseRule(ABC):
    """规则基类：命中则返回 RuleResult，未命中返回 None。"""

    name: str = ""

    @abstractmethod
    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        ...


_registry: dict[str, BaseRule] = {}


def register(cls):
    """装饰器：将规则类按 name 注册并实例化。"""
    _registry[cls.name] = cls()
    return cls


def all_rules() -> list[BaseRule]:
    return list(_registry.values())