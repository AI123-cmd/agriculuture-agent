"""R-CAUTION 表述语气与 R-NO-DATA 无数据支撑数字（T2-44）。

两条规则同文件：它们都读同一份展示文本与溯源表，是"表述层"的两道检查。`R-CAUTION` 是
`warn` 级（语气问题不致命，但会改变农户的风险感知），`R-NO-DATA` 是 `block` 级。
"""

from typing import ClassVar

from app.gate.constants import IMPERATIVE_PATTERNS
from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register
from app.gate.rules.provenance import STRUCTURAL_EXEMPTIONS, UNIT_PATTERN


@register
class CautionToneRule(Rule):
    """R-CAUTION：输出不得为强制命令语气，须保留建议性与免责表述。

    等级为 warn：语气问题不致命，但会改变农户的风险感知。强制命令语气会让农户把"建议"
    当作"指令"执行，出事时责任归属模糊。
    """

    rule_id = GateRuleId.CAUTION.value
    severity: ClassVar[GateSeverity] = "warn"
    description = "输出不得为强制命令语气"

    REQUIRED_SOFTENERS: ClassVar[tuple[str, ...]] = (
        "建议",
        "可以",
        "如果",
        "建议咨询",
        "可考虑",
        "不妨",
        "必要时",
    )

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        text = ctx.payload.rendered_text
        for pattern in IMPERATIVE_PATTERNS:
            for match in pattern.finditer(text):
                violations.append(
                    self.violation(
                        field="gate_payload.rendered_text",
                        message=f"出现强制命令语气「{match.group(0)}」，应改为建议性表述",
                    )
                )
        if not any(s in text for s in self.REQUIRED_SOFTENERS):
            violations.append(
                self.violation(
                    field="gate_payload.rendered_text",
                    message="整段建议未出现任何建议性措辞（建议/可以/如果…），确定性表述偏高",
                )
            )
        return violations


@register
class NoDataSupportRule(Rule):
    """R-NO-DATA：处方结构中的数值字段必须在 `facts_provenance` 中登记。

    与 `R-PROVENANCE` 的分工：本规则是正向核对（从数值字段找漏登记），后者是反向扫描
    （从展示文本找多出来的数字）。两者互补，缺一不可。
    """

    rule_id = GateRuleId.NO_DATA.value
    severity: ClassVar[GateSeverity] = "block"
    description = "不得出现无事实来源的数值字段"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        provenance = ctx.prescription.facts_provenance
        for path in ctx.prescription.numeric_paths():
            if path in provenance and str(provenance[path]).strip():
                continue
            violations.append(self.violation(field=path, message=f"数值字段 {path} 缺少事实来源登记"))
        violations.extend(self._scan_text(ctx, provenance))
        return violations

    @staticmethod
    def _scan_text(ctx: GateContext, provenance: dict[str, str]) -> list[GateViolation]:
        """第二道：展示文本中带单位的数字若既无 token 登记，其值也不在 provenance 来源中。"""
        out: list[GateViolation] = []
        registered_sources = set(provenance.values())
        tokens = ctx.registered_numeric_tokens()
        for match in UNIT_PATTERN.finditer(ctx.payload.rendered_text):
            number, unit = match.group(1), match.group(2)
            token = f"{number}{unit}"
            if token in tokens:
                continue
            if number in STRUCTURAL_EXEMPTIONS:
                continue
            if any(number in str(s) for s in registered_sources):
                continue
            out.append(
                GateViolation(
                    rule_id=GateRuleId.NO_DATA.value,
                    severity="block",
                    message=f"文本数值「{token}」无事实来源登记",
                    field="gate_payload.rendered_text",
                )
            )
        return out


__all__ = ["CautionToneRule", "NoDataSupportRule"]
