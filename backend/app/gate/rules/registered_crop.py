"""R-REGISTERED-CROP 登记作物匹配（T2-36）。

药剂登记作物必须包含当前流程对应作物。茶流程要求最严：药剂需同时满足已登记 + 安全间隔期
+ 残留限量三重条件。
"""

from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity, ProcessId
from app.gate.rules.base import Rule, register


@register
class RegisteredCropRule(Rule):
    """R-REGISTERED-CROP：药剂登记作物必须包含当前流程对应作物。"""

    rule_id = GateRuleId.REGISTERED_CROP.value
    severity: ClassVar[GateSeverity] = "block"
    description = "药剂登记作物必须包含当前流程对应作物"

    # 流程 → 登记作物名（植保口径）。杂草/通用/养猪不在映射内，不做作物登记匹配。
    CROP_MAPPING: ClassVar[dict[ProcessId, str]] = {
        ProcessId.TEA: "茶",
        ProcessId.RICE: "水稻",
        ProcessId.CHILI: "辣椒",
        ProcessId.RAPESEED: "油菜",
    }

    def check(self, ctx: GateContext) -> list[GateViolation]:
        registered = self.CROP_MAPPING.get(ctx.process_id)
        if registered is None:
            return []
        violations: list[GateViolation] = []
        for item in ctx.prescription.pesticide_items:
            crops = ctx.knowledge.get_registered_crops(item.knowledge_id)
            if registered not in crops:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{item.knowledge_id}]",
                        message=(f"药剂《{item.name}》未在{registered}上登记（登记作物：{'、'.join(crops) or '无'}）"),
                    )
                )
        return violations


__all__ = ["RegisteredCropRule"]
