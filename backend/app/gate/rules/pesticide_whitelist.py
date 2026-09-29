"""R-WHITELIST 药剂白名单（T2-35）。

药剂条目必须来自已核准知识库。这条排在最前：知识库是"事实"的唯一合法来源，一旦允许模型
生成不在库中的药剂，后续所有剂量、间隔期、登记作物校验都失去比对基准。
"""

from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register


@register
class PesticideWhitelistRule(Rule):
    """R-WHITELIST：药剂条目必须来自已核准知识库。"""

    rule_id = GateRuleId.WHITELIST.value
    severity: ClassVar[GateSeverity] = "block"
    description = "药剂条目必须存在于已核准知识库"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        for item in ctx.prescription.pesticide_items:
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            if entry is None:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{item.knowledge_id}]",
                        message=f"药剂《{item.name}》不在已核准知识库中（条目 {item.knowledge_id} 不存在）",
                    )
                )
                continue
            if not ctx.knowledge.is_approved(item.knowledge_id):
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{item.knowledge_id}]",
                        message=(
                            f"药剂《{item.name}》引用的条目 {item.knowledge_id} 当前状态为 "
                            f"{entry.status}，不可用于处方"
                        ),
                    )
                )
                continue
            if entry.name != item.name:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{item.knowledge_id}].name",
                        message=(
                            f"药剂名与知识库不一致：处方为《{item.name}》，"
                            f"条目 {item.knowledge_id} 为《{entry.name}》"
                        ),
                    )
                )
        return violations


__all__ = ["PesticideWhitelistRule"]
