"""R-INTERVAL 安全间隔期（T2-38）。

安全间隔期必须明确，且不得与采摘节奏冲突。产品说明书 5.3：不得开出未核算安全间隔期与每季
最多使用次数的方案；不得开出与采摘节奏冲突的方案——茶农面临的不只是减产，而是"采不了"。

三条硬约束：安全间隔期必须显式给出；必须与下一轮采摘计划核算不冲突；缺 `next_pick_date`
即 block（fail-closed：农户没说何时采，系统就无法证明方案不冲突）。
"""

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity, ProcessId
from app.gate.rules.base import Rule, register


@register
class SafetyIntervalRule(Rule):
    """R-INTERVAL：安全间隔期必须明确，且不得与采摘节奏冲突。"""

    rule_id = GateRuleId.INTERVAL.value
    severity: GateSeverity = "block"
    description = "安全间隔期必须明确且与采摘计划不冲突"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []

        # 1. 安全间隔期必须显式给出（杂草流程允许依据标签，但标签本身即知识条目）
        if ctx.prescription.pesticide_items and ctx.prescription.safety_interval_days is None:
            violations.append(
                self.violation(field="safety_interval_days", message="涉及用药但未给出安全间隔期")
            )

        # 2. 茶流程：与下一轮采摘计划核对。槽位经 slot_date 归一，非日期一律视为缺失。
        if ctx.process_id == ProcessId.TEA:
            next_pick = ctx.slot_date("next_pick_date")
            if ctx.prescription.pesticide_items and next_pick is None:
                violations.append(
                    self.violation(
                        field="slots.next_pick_date",
                        message="茶流程缺少下一轮采摘计划，无法核算安全间隔期",
                    )
                )
            elif next_pick is not None and ctx.prescription.safety_interval_days:
                days_to_pick = (next_pick - ctx.today).days
                if days_to_pick < ctx.prescription.safety_interval_days:
                    violations.append(
                        self.violation(
                            field="safety_interval_days",
                            message=(
                                f"距下一轮采摘仅 {days_to_pick} 天，"
                                f"短于安全间隔期 {ctx.prescription.safety_interval_days} 天"
                            ),
                        )
                    )

        # 3. 配置下限核对（TEA-03 的 R-INTERVAL 分支）：适用于全部流程
        floor = self._configured_floor(ctx)
        if floor is not None and ctx.prescription.safety_interval_days is not None:
            if ctx.prescription.safety_interval_days < floor:
                violations.append(
                    self.violation(
                        field="safety_interval_days",
                        message=(
                            f"安全间隔期 {ctx.prescription.safety_interval_days} 天"
                            f"短于本流程配置下限 {floor} 天"
                        ),
                    )
                )
        return violations

    @staticmethod
    def _configured_floor(ctx: GateContext) -> int | None:
        """红线配置给出的间隔期下限（`data.min_days`）。"""
        raw = ctx.rule_configs.get(GateRuleId.INTERVAL.value, {}).get("min_days")
        return int(raw) if isinstance(raw, (int, str)) and str(raw).lstrip("-").isdigit() else None


__all__ = ["SafetyIntervalRule"]
