"""R-REGION 地区适宜性（T2-40）。

等级为 `degrade`：地区不匹配的正确处置是引导农户咨询本地农技站，而不是拒绝服务——
农户所在地区与知识条目适用范围不一致，不代表建议有害。这是 8.2 中唯一以 degrade 为默认
等级的规则。
"""

from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register


@register
class RegionFitnessRule(Rule):
    """R-REGION：建议必须与所在地地区、当前阶段匹配。"""

    rule_id = GateRuleId.REGION.value
    severity: ClassVar[GateSeverity] = "degrade"
    description = "建议必须与所在地地区匹配"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        if ctx.region_code:
            violations.extend(self._check_knowledge_scope(ctx))
        violations.extend(self._check_declared_regions(ctx))
        violations.extend(self._check_export_standards(ctx))
        return violations

    def _check_knowledge_scope(self, ctx: GateContext) -> list[GateViolation]:
        """药剂条目的 `region_scope` 必须覆盖农户所在地区。"""
        out: list[GateViolation] = []
        scope = _region_prefix(ctx.region_code or "")
        for item in ctx.prescription.pesticide_items:
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            if entry is None or not entry.region_scope:
                continue  # 无地区限定＝全国通用
            if not _matches_prefix(scope, entry.region_scope):
                out.append(
                    self.violation(
                        field=f"pesticide_items[{item.knowledge_id}].region_scope",
                        message=(f"《{item.name}》的适用范围为 {entry.region_scope}，不覆盖你所在的 {ctx.region_code}"),
                    )
                )
        return out

    def _check_declared_regions(self, ctx: GateContext) -> list[GateViolation]:
        """红线配置声明的适用地区清单核对（`data.regions`）。"""
        raw = ctx.rule_configs.get(GateRuleId.REGION.value, {}).get("regions")
        if not raw or not ctx.region_code:
            return []
        allowed = [str(r) for r in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
        scope = _region_prefix(ctx.region_code)
        if not any(_matches_prefix(scope, a) for a in allowed):
            return [
                self.violation(
                    field="region_code",
                    message=f"本流程建议的适用地区为 {'、'.join(allowed)}，与 {ctx.region_code} 不一致",
                )
            ]
        return []

    def _check_export_standards(self, ctx: GateContext) -> list[GateViolation]:
        """TEA-05：出口茶园不得忽略输入国标准。`data.standards` 声明需要的标准。

        地区未知时整条 R-REGION 不参与判定（三条分支均以"农户所在地区已知"为前提）。
        """
        if not ctx.region_code:
            return []
        raw = ctx.rule_configs.get(GateRuleId.REGION.value, {}).get("standards")
        if not raw:
            return []
        standards = [str(s) for s in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
        covered = ctx.prescription.facts_provenance.values()
        missing = [s for s in standards if not any(s in str(v) for v in covered)]
        if missing:
            return [
                self.violation(
                    field="facts_provenance",
                    message=(f"出口茶园处方未引用输入国/地区标准 {'、'.join(missing)}，无法确认残留限量合规"),
                )
            ]
        return []


def _region_prefix(code: str) -> str:
    """取行政区划编码的稳定前缀。省 2 位、市 4 位、县 6 位。

    去掉尾随的 0 后，`330000`（省）与 `330000` 的县级写法 `330100` 会在同一层级对齐：
    省级 `33`、市级 `3301`、县级 `330101`。这样"上级编码覆盖下级"才可以比较。
    """
    digits = "".join(ch for ch in code if ch.isdigit())
    return digits.rstrip("0")


def _matches_prefix(scope: str, entry_scope: str) -> bool:
    """上级编码覆盖下级：条目范围 330000 覆盖农户 330100。

    两侧都去掉尾随 0 后互为前缀即为覆盖；`330100` 与 `3301`（同市）也命中。
    """
    a, b = _region_prefix(scope), _region_prefix(entry_scope)
    if not a or not b:
        return True  # 无法比对时不报（degrade 规则的保守方向）
    return a.startswith(b) or b.startswith(a)


__all__ = ["RegionFitnessRule"]
