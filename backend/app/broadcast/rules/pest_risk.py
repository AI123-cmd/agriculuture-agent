"""温湿组合风险规则（T3-05，技术方案 11.1）。

单一指标越界未必构成风险，高温高湿的**组合**才是病害流行的经典条件。本规则用组合判据
产出一条 `env_risk` 条目，与六条单指标阈值规则并列。

**必须双指标可用**：温度与湿度任一缺失即整条不产出。这条前置判断与环境不可用的拦截是
两回事——环境事实可能来自天气服务（`source != "none"`）却只覆盖温度字段，此时湿度缺失
若被当成"湿度为 0"参与比较，会产出与实际相反的结论。缺一指标就沉默，符合"不用推测值
生成"（产品说明书 6.4 红线四）。
"""

from app.broadcast.rules.base import BaseRule, RuleResult
from app.broadcast.rules.engine import RuleContext
from app.broadcast.rules.threshold import SOURCE_TIMELINESS, callout_of, resolve_threshold, unit_of
from app.broadcast.rules.urgency import is_urgent
from app.domain.metrics import StandardMetric

# 温湿组合的默认阈值：高温阈值与高湿阈值同时满足才命中。
_DEFAULT_TEMP_HIGH = 30.0
_DEFAULT_HUMIDITY_HIGH = 80.0


class PestRiskRule(BaseRule):
    """温湿组合规则：高温 且 高湿同时成立即命中。

    `requires_env=True`；组合阈值支持流程配置覆盖（`timing_rules` 的 `air_temp_above` /
    `air_humidity_above`）。双指标任一缺失即返回空——`evaluate` 可被单独调用，这条前置
    判断不可省。
    """

    requires_env = True
    source_kind = "env_risk"

    rule_id = "env_risk.pest_risk"

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        temp = ctx.env.values.get(StandardMetric.AIR_TEMP.value)
        humidity = ctx.env.values.get(StandardMetric.AIR_HUMIDITY.value)
        if temp is None or humidity is None:
            return []  # 必须双指标可用，缺一即沉默（不做"缺失=0"的错误比较）

        temp_threshold = resolve_threshold(
            ctx.process_config, StandardMetric.AIR_TEMP.value, "above", _DEFAULT_TEMP_HIGH
        )
        humidity_threshold = resolve_threshold(
            ctx.process_config, StandardMetric.AIR_HUMIDITY.value, "above", _DEFAULT_HUMIDITY_HIGH
        )
        if temp <= temp_threshold or humidity <= humidity_threshold:
            return []

        severity = "urgent" if is_urgent(self.rule_id, ctx.env, ctx.process_config, stage=ctx.stage) else "notice"
        temp_unit = unit_of(StandardMetric.AIR_TEMP.value)
        humidity_unit = unit_of(StandardMetric.AIR_HUMIDITY.value)
        return [
            RuleResult(
                source_kind=self.source_kind,
                severity=severity,
                callout=callout_of(ctx),
                fact=(
                    f"气温 {temp:.1f}{temp_unit} 且空气湿度 {humidity:.1f}{humidity_unit}，处于病害易流行的温湿组合区间"
                ),
                impact="高温高湿利于病原菌繁殖与侵染，密闭或通风不良地块风险更高",
                action="加强通风降湿，巡查病斑，必要时按流程要点预防",
                rule_id=self.rule_id,
                facts_provenance={
                    StandardMetric.AIR_TEMP.value: ctx.env.provenance.get(
                        StandardMetric.AIR_TEMP.value, ctx.env.source
                    ),
                    StandardMetric.AIR_HUMIDITY.value: ctx.env.provenance.get(
                        StandardMetric.AIR_HUMIDITY.value, ctx.env.source
                    ),
                },
                timeliness_score=SOURCE_TIMELINESS.get(ctx.env.source, 0.0),
                relevance_score=0.6,
            )
        ]


__all__ = ["PestRiskRule"]
