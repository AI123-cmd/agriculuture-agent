"""R-REDLINE-LOAD 红线配置加载校验（T2-43）。

产品说明书 9.3：流程专属红线不可缺省，缺失即不可用，禁止以通用规则代替。因此本规则不检查
处方内容——前提不成立时，任何关于处方的判断都不该被相信。

整条规则只有两个分支，这是刻意的：它必须足够简单到无法出错，因为它决定的是"其余校验结果
是否可信"。它是引擎排序第一位，也是短路点（见 `engine.py`）。
"""

from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register


@register
class RedlineLoadRule(Rule):
    """R-REDLINE-LOAD：流程红线配置必须成功加载，否则直接 block。"""

    rule_id = GateRuleId.REDLINE_LOAD.value
    severity: ClassVar[GateSeverity] = "block"
    description = "流程红线配置必须成功加载"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        if ctx.redlines.loaded:
            return []
        reason = ctx.redlines.unavailable_reason or "未给出原因"
        return [
            self.violation(
                field="process_redlines_loaded",
                message=(
                    f"流程 {ctx.process_id.value} 的红线配置未成功加载（{reason}），按不可用处置，不降级为通用规则"
                ),
            )
        ]


__all__ = ["RedlineLoadRule"]
