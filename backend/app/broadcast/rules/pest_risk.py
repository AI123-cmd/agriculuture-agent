"""病虫害气象风险规则：温湿组合判断，比单阈值更准确。"""

from typing import TYPE_CHECKING

from .base import BaseRule, RuleResult, register

if TYPE_CHECKING:
    from app.broadcast.snapshot import EnvSnapshot
    from app.core.config import Settings


@register
class PestRiskRule(BaseRule):
    """高湿适温条件下，叶部病害发生风险升高。"""

    name = "pest_risk"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        if snapshot.temp is None or snapshot.humidity is None:
            return None
        # 高湿适温（如玉米大斑病 20~25℃ + 湿度≥90% 等组合）
        if snapshot.humidity >= 90 and 20 <= snapshot.temp <= 25:
            return RuleResult(
                type="pest",
                level="warning",
                title="叶部病害发生风险升高",
                content=(
                    f"当前湿度 {snapshot.humidity:.0f}%、气温 {snapshot.temp:.0f}℃，"
                    "高湿适温条件利于叶部病害发生流行，请加强田间巡查。"
                ),
                source="sensor",
                action="加强巡查，必要时提前预防性用药，注意通风排湿",
            )
        return None
