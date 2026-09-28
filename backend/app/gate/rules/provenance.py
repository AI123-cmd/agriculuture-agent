"""R-PROVENANCE 数字溯源（T2-41）。

对输出的展示文本做数字抽取（含单位），逐个核对是否在 provenance 表中有对应登记。这是
**反向扫描**：从文本找数字。与之互补的 `R-NO-DATA`（T2-44）是**正向核对**：从数值字段找
漏登记。前者拦住"文本里多出来的数字"（模型改写时插入的数字不会出现在任何字段里），后者拦住
"结构里漏登记"。两条共用 `UNIT_PATTERN` 与 `STRUCTURAL_EXEMPTIONS`，但不共用 `check`。

中文数字的处理边界：`UNIT_PATTERN` 只匹配阿拉伯数字。展示层约定所有事实性数字使用阿拉伯
数字（处方各字段由代码填充），因此中文数字（"七天"、"三亩"）不会出现；若未来展示层引入
中文数字，需新增一条规则而非放宽本正则。
"""

import re

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register

# 单位白名单：这些词后面的数字属于必须溯源的"事实性数字"。
UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(亩|克|毫升|千克|公斤|升|%|℃|天|小时|次|头|株|丛|片)")

# 结构性数字豁免：仅限处方卡的步骤序号 1~5，带单位的事实性数字一律不豁免。
# 该集合的每个成员都必须能对应到输出模板里的一个具体位置，否则不许加入。
# 技术方案 8.3.3："豁免清单必须极短且有明确语义，否则会变成漏洞。"
STRUCTURAL_EXEMPTIONS: frozenset[str] = frozenset({"1", "2", "3", "4", "5"})
_STRUCTURAL_MAX = 5


@register
class ProvenanceRule(Rule):
    """R-PROVENANCE：处方文本中的每个数字必须在 provenance 表中有登记。"""

    rule_id = GateRuleId.PROVENANCE.value
    severity: GateSeverity = "block"
    description = "文本中的事实性数字必须有事实来源登记"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        text = ctx.payload.rendered_text
        registered_values = set(ctx.registered_numeric_tokens())

        for match in UNIT_PATTERN.finditer(text):
            number, unit = match.group(1), match.group(2)
            token = f"{number}{unit}"
            if token in registered_values or number in STRUCTURAL_EXEMPTIONS:
                continue
            violations.append(
                self.violation(
                    field="facts_provenance",
                    message=f"文本中出现的数值「{token}」无事实来源登记",
                )
            )
        return violations


__all__ = ["STRUCTURAL_EXEMPTIONS", "ProvenanceRule", "UNIT_PATTERN"]
