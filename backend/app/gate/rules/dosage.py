"""R-DOSAGE 剂量区间与 R-MAX-USE 每季次数（T2-37）。

两条规则同文件：它们都从知识条目读取登记值，共用一个"取条目"的前置步骤，分文件会让读者
以为它们互相独立。二者回答不同问题——`R-DOSAGE` 是"量对不对"，`R-MAX-USE` 是"次数超没超"。
"""

from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register


@register
class DosageRangeRule(Rule):
    """R-DOSAGE：剂量必须落在知识条目给定的 [min, max] 区间内。"""

    rule_id = GateRuleId.DOSAGE.value
    severity: ClassVar[GateSeverity] = "block"
    description = "剂量必须落在知识条目给定的区间内"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        for i, item in enumerate(ctx.prescription.pesticide_items):
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            if entry is None:
                continue  # 条目不存在由 R-WHITELIST 拦截，本规则不重复报
            lo, hi = entry.dosage_min, entry.dosage_max
            if item.dosage_min is None and item.dosage_max is None:
                continue  # 处方未给剂量：由 R-INTERVAL 与展示层处理
            unit = entry.dosage_unit or ""
            if lo is not None and item.dosage_min is not None and item.dosage_min < lo:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}].dosage_min",
                        message=f"《{item.name}》用药量 {item.dosage_min}{unit} 低于登记下限 {lo}{unit}",
                    )
                )
            if hi is not None and item.dosage_max is not None and item.dosage_max > hi:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}].dosage_max",
                        message=f"《{item.name}》用药量 {item.dosage_max}{unit} 高于登记上限 {hi}{unit}",
                    )
                )
            if (
                lo is not None
                and hi is not None
                and item.dosage_min is not None
                and item.dosage_max is not None
                and item.dosage_min > item.dosage_max
            ):
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}].dosage_min",
                        message=f"《{item.name}》剂量区间上下限颠倒：{item.dosage_min} > {item.dosage_max}",
                    )
                )
        return violations


@register
class MaxUsePerSeasonRule(Rule):
    """R-MAX-USE：每季次数不得超出登记值，且与用药史累计不超限。"""

    rule_id = GateRuleId.MAX_USE.value
    severity: ClassVar[GateSeverity] = "block"
    description = "每季使用次数与登记值、用药史累计均不得超限"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        limit = self._configured_limit(ctx)
        used = self._history_used(ctx)
        for i, item in enumerate(ctx.prescription.pesticide_items):
            declared = item.max_uses_per_season
            if declared is not None and limit is not None and declared > limit:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}].max_uses_per_season",
                        message=f"《{item.name}》每季 {declared} 次超出登记上限 {limit} 次",
                    )
                )
            hit = used.get(item.knowledge_id, 0)
            bound = limit if limit is not None else declared
            if bound is not None and hit + 1 > bound:
                violations.append(
                    self.violation(
                        field=f"pesticide_items[{i}].max_uses_per_season",
                        message=f"《{item.name}》本季已用 {hit} 次，再加本次将超过上限 {bound} 次",
                    )
                )
        top = ctx.prescription.max_uses_per_season
        if top is not None and limit is not None and top > limit:
            violations.append(
                self.violation(
                    field="max_uses_per_season",
                    message=f"处方顶层每季次数 {top} 次超出登记上限 {limit} 次",
                )
            )
        return violations

    @staticmethod
    def _configured_limit(ctx: GateContext) -> int | None:
        """红线配置给出的次数上限（`data.limit`）。"""
        raw = ctx.rule_configs.get(GateRuleId.MAX_USE.value, {}).get("limit")
        return int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None

    @staticmethod
    def _history_used(ctx: GateContext) -> dict[str, int]:
        """统计本季用药史中每个 `knowledge_id` 的已用次数。"""
        out: dict[str, int] = {}
        start = ctx.season_start
        for rec in ctx.medication_history:
            if start is not None and rec.used_on < start:
                continue
            if rec.knowledge_id:
                out[rec.knowledge_id] = out.get(rec.knowledge_id, 0) + 1
        return out


__all__ = ["DosageRangeRule", "MaxUsePerSeasonRule"]
