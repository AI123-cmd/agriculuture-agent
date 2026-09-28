"""R-CONFLICT 用药史冲突（T2-39）。

处方推荐的药剂与档案记录的近期用药之间，不得存在重复用药或已知禁忌搭配。它与 `R-MAX-USE`
读同一份用药史但回答不同问题——次数超限是"量"，重复与禁忌是"质"。
"""

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.matching import conflicts_with, identity_key, recent_records
from app.gate.rules.base import Rule, register


@register
class MedicationConflictRule(Rule):
    """R-CONFLICT：不得与档案近期用药重复，也不得构成已知禁忌搭配。"""

    rule_id = GateRuleId.CONFLICT.value
    severity: GateSeverity = "block"
    description = "不得与近期用药重复或构成禁忌搭配"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        recent = recent_records(ctx.medication_history, ctx.season_start)
        if not recent:
            return violations
        used_keys = {identity_key(r.pesticide_name): r for r in recent if r.pesticide_name}

        for i, item in enumerate(ctx.prescription.pesticide_items):
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            key = identity_key(item.name)
            prev = used_keys.get(key)
            if prev is not None:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}]",
                        message=(
                            f"《{item.name}》与 {prev.used_on.isoformat()} 的用药重复，"
                            f"连续使用同一药剂会加速抗药性"
                        ),
                    )
                )
            combos = entry.forbidden_combinations if entry is not None else ()
            hits = conflicts_with(combos, recent)
            if hits:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}].forbidden_combinations",
                        message=f"《{item.name}》与近期用过的 {'、'.join(hits)} 属已知禁忌搭配",
                    )
                )
        return violations


__all__ = ["MedicationConflictRule"]
