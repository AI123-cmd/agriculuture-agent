"""规则层契约包（T3-05 起）。

`base.py` 承载规则层共享的 `RuleResult` 与 `BaseRule`；`engine.py` 在其上实现
`RuleEngine` 与 `RuleContext`；`catalog.py` 装配规则实例；`threshold.py` / `pest_risk.py`
是环境事实类的具体规则；`urgency.py` 提供 T3-06 的紧急双条件判定；`errors.py` 定义实现期
异常；`templates.py` 提供 T3-07 的事实模板。本包不含任何模型调用。

只在此处导出契约、引擎入口与模板入口：具体规则类通过 `catalog.load_rules()` 取用，避免
调用方与某一条规则的实现细节耦合。
"""

from app.broadcast.rules.base import BaseRule, RuleResult
from app.broadcast.rules.engine import RuleContext, RuleEngine, validate_item
from app.broadcast.rules.errors import RuleImplementationError
from app.broadcast.rules.templates import RULE_TEMPLATES, render_template, template_key
from app.broadcast.rules.urgency import is_urgent

__all__ = [
    "RULE_TEMPLATES",
    "BaseRule",
    "RuleContext",
    "RuleEngine",
    "RuleImplementationError",
    "RuleResult",
    "is_urgent",
    "render_template",
    "template_key",
    "validate_item",
]
