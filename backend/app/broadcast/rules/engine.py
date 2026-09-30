"""规则层匹配引擎（T3-05，技术方案 11.1）。

把三类输入（环境事实、农事历、问诊回访）变成结构化的提示条目：规则层负责"哪些输入命中
哪条规则、命中后条目长什么样"，全是纯确定性代码，**任何路径都不调用模型**。

零模型模式的实现方式是**结构性**的而不是开关式的：`RuleEngine` 的构造签名里没有任何
模型网关参数，`match_all` 的函数体内不存在协程调用，因此"规则层调了模型"这件事在不改
函数签名的前提下不可能发生。开关会被人打开，结构性缺失不会。

`RuleContext.env` 的 `source == "none"` 时，标了 `requires_env` 的规则被整体跳过
（技术方案 11.2）：环境不可用时不做环境风险类提示，而不是用推测值生成。跳过点落在引擎侧
与规则侧两处——规则也可被单独调用（测试、影子模式），那时引擎的拦截不生效，规则内部仍需
判空。

`validate_item` 是引擎出口的统一完整性校验：五段式缺一段即视为规则实现错误并抛异常。
集中校验而非让每条规则各自保证，是因为规则会被不断新增，靠约定维持字段完整性的成本远高于
一个校验点。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agent.state import ProcessId
from app.broadcast.rules.base import BaseRule, RuleResult
from app.broadcast.rules.errors import RuleImplementationError

if TYPE_CHECKING:  # 仅供参考的类型标注，运行时不导入，避免规则与输入层互相依赖
    from app.broadcast.inputs.env_facts import EnvFacts
    from app.process_config.models import ProcessConfig

# 五段式中由规则负责的四段（级别另有枚举约束，由 ORM 层拦）。
_REQUIRED_FIELDS = ("callout", "fact", "impact", "action")


@dataclass(frozen=True)
class RuleContext:
    """一次规则匹配的输入上下文。

    `env` 是环境事实（含四级来源与扁平指标映射）；`calendar` / `signals` 是农事历与问诊
    回访两路已构造好的条目，引擎把它们与规则命中合并后一并返回——这两路本身就是确定性
    产出，不再经规则二次加工（技术方案 11.2）。

    `process_id` 与 `process_config` 决定哪些规则适用、各流程的阈值与紧急判定参数；
    `stage` 是当前生育期，供 T3-06 的 `is_urgent` 第二条件使用。`object_label` 是环境风险
    类条目的呼告对象（如"东边水田"），缺省时由规则回落到流程显示名。
    """

    env: "EnvFacts"
    process_id: ProcessId = ProcessId.GENERAL
    process_config: "ProcessConfig | None" = None
    calendar: Sequence[RuleResult] = ()
    signals: Sequence[RuleResult] = ()
    stage: str | None = None
    object_label: str | None = None


def validate_item(item: RuleResult) -> RuleResult:
    """落库前的完整性校验：五段式缺一段即视为规则实现错误。"""
    for name in _REQUIRED_FIELDS:
        value = getattr(item, name)
        if not value or not value.strip():
            raise RuleImplementationError(f"{item.rule_id} 缺少 {name}")
    return item


class RuleEngine:
    """规则层匹配引擎：纯确定性，任何路径都不调用模型。"""

    def __init__(self, rules: list[BaseRule] | None = None) -> None:
        if rules is None:
            from app.broadcast.rules.catalog import load_rules

            rules = load_rules()
        self.rules = rules

    def match_all(self, ctx: RuleContext) -> list[RuleResult]:
        """三类输入 → 全部命中条目。此函数在零模型模式下行为完全一致。"""
        results: list[RuleResult] = []
        for rule in self.rules:
            if not rule.applies_to(ctx.process_id):
                continue
            if rule.requires_env and ctx.env.source == "none":
                continue  # 11.2：环境不可用时该路条目在生成阶段拦截
            for item in rule.evaluate(ctx):
                results.append(validate_item(item))
        for item in (*ctx.calendar, *ctx.signals):
            results.append(validate_item(item))
        return results


__all__ = ["RuleContext", "RuleEngine", "validate_item"]
