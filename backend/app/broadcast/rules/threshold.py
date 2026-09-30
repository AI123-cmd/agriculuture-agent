"""环境阈值规则（T3-05，技术方案 11.1）。

六条阈值规则（高温、低温、土壤墒情、湿度、降雨、风速）把环境事实里的实测值与阈值比较，
命中即产出一条 `env_risk` 类条目。三处设计：

1. **阈值从流程配置取，`BROADCAST_` 全局配置作兜底**：流程的 `timing_rules[*].when` 里
   `<指标>_above` / `<指标>_below` 形式的键是该流程的阈值覆盖（技术方案 11.3）。以水稻
   为例，`rice.yaml` 的 `{air_temp_above: 35}` 覆盖全局的 `BROADCAST_TEMP_HIGH`。取不到
   覆盖值时回落到全局标量——配置迁移动辄涉及七份 YAML，兜底让未迁移的流程照常工作。
2. **`requires_env=True`**：`EnvFacts.source == "none"` 时该路条目在生成阶段被拦截
   （技术方案 11.2）。规则内部仍逐指标判空，因为规则可被单独调用（测试、影子模式），
   那时引擎的拦截不生效。
3. **级别由 T3-06 的 `is_urgent` 双条件判定**：命中阈值只代表"越过临界"，是否升到
   `urgent` 要同时满足"严重偏离"与"处于敏感生育期"。规则层不自行拍级别，参数来自流程配置。

规则不硬编码流程分支（按流程标识逐条判断），流程差异一律落在配置里。
"""

from app.broadcast.rules.base import BaseRule, RuleResult
from app.broadcast.rules.engine import RuleContext
from app.broadcast.rules.urgency import is_urgent
from app.core.config import get_settings
from app.domain.metrics import DEFAULT_UNITS, StandardMetric
from app.process_config.models import ProcessConfig

# 环境来源 → 时效性分数（T3-06 排序第二维）。实时读数最高，参考值次之，天气再次。
SOURCE_TIMELINESS: dict[str, float] = {
    "sensor": 1.0,
    "weather": 0.8,
    "sensor_stale": 0.5,
    "none": 0.0,
}

# 阈值命中条目的呼告对象取不到时的兜底显示名。
_DEFAULT_CALLOUT = "当前地块"

# 全局兜底阈值（`BROADCAST_` 六项），键为 `(metric, direction)`。流程配置未覆盖时使用。
_GLOBAL_FALLBACK: dict[tuple[str, str], str] = {
    (StandardMetric.AIR_TEMP.value, "above"): "BROADCAST_TEMP_HIGH",
    (StandardMetric.AIR_TEMP.value, "below"): "BROADCAST_TEMP_LOW",
    (StandardMetric.SOIL_MOISTURE.value, "below"): "BROADCAST_SOIL_DRY",
    (StandardMetric.AIR_HUMIDITY.value, "above"): "BROADCAST_HUMIDITY_HIGH",
    (StandardMetric.RAINFALL.value, "above"): "BROADCAST_RAINFALL_MM",
    (StandardMetric.WIND_SPEED.value, "above"): "BROADCAST_WIND_MS",
}


def resolve_threshold(config: ProcessConfig | None, metric: str, direction: str, default: float) -> float:
    """按"流程配置覆盖 → 全局兜底 → 代码默认"三级取阈值。

    `timing_rules[*].when` 里 `<metric>_above` / `<metric>_below` 形式的键是流程覆盖值；
    解析不出时回落到 `BROADCAST_` 全局配置，再取不到才用 `default`。
    """
    if config is not None:
        key = f"{metric}_{direction}"
        for rule in config.timing_rules:
            value = rule.when.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    setting_name = _GLOBAL_FALLBACK.get((metric, direction))
    if setting_name is not None:
        return float(getattr(get_settings(), setting_name))
    return default


def unit_of(metric: str) -> str:
    """指标默认单位（展示用）；非标准指标返回空串。"""
    try:
        return DEFAULT_UNITS[StandardMetric(metric)]
    except ValueError:
        return ""


def callout_of(ctx: RuleContext) -> str:
    """条目的呼告对象：优先对象名，其次流程显示名，最后兜底。"""
    if ctx.object_label:
        return ctx.object_label
    if ctx.process_config is not None:
        return ctx.process_config.display_name
    return _DEFAULT_CALLOUT


class ThresholdRule(BaseRule):
    """单条环境阈值规则：实测值越界即命中。

    `requires_env=True`（环境不可用不产出）；`process_ids=None`（全流程适用，差异落在
    阈值配置里而非流程分支里）。`direction="above"` 表示指标高于阈值即命中，`"below"`
    反之。`rule_id` 是稳定标识，同时是 T3-06 `urgency_specs` 的键。
    """

    requires_env = True
    source_kind = "env_risk"

    def __init__(
        self,
        rule_id: str,
        metric: str,
        direction: str,
        *,
        default_threshold: float,
        fact_template: str,
        impact: str,
        action: str,
    ) -> None:
        self.rule_id = rule_id
        self.metric = metric
        self.direction = direction
        self.default_threshold = default_threshold
        self.fact_template = fact_template
        self.impact = impact
        self.action = action

    def _hit(self, value: float, threshold: float) -> bool:
        if self.direction == "above":
            return value > threshold
        return value < threshold

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        value = ctx.env.values.get(self.metric)
        if value is None:
            return []  # 规则可被单独调用，此处仍需判空（引擎的拦截不覆盖直接调用）
        threshold = resolve_threshold(ctx.process_config, self.metric, self.direction, self.default_threshold)
        if not self._hit(value, threshold):
            return []
        severity = "urgent" if is_urgent(self.rule_id, ctx.env, ctx.process_config, stage=ctx.stage) else "notice"
        unit = unit_of(self.metric)
        return [
            RuleResult(
                source_kind=self.source_kind,
                severity=severity,
                callout=callout_of(ctx),
                fact=self.fact_template.format(value=value, threshold=threshold, unit=unit),
                impact=self.impact,
                action=self.action,
                rule_id=self.rule_id,
                facts_provenance={self.metric: ctx.env.provenance.get(self.metric, ctx.env.source)},
                timeliness_score=SOURCE_TIMELINESS.get(ctx.env.source, 0.0),
                relevance_score=0.5,
            )
        ]


def default_threshold_rules() -> list[BaseRule]:
    """六条阈值的默认实例。阈值取自全局配置的默认值，运行时按流程配置覆盖。"""
    settings = get_settings()
    return [
        ThresholdRule(
            "env_risk.heat",
            StandardMetric.AIR_TEMP.value,
            "above",
            default_threshold=settings.BROADCAST_TEMP_HIGH,
            fact_template="气温达到 {value:.1f}{unit}，超过高温阈值 {threshold:.1f}{unit}",
            impact="持续高温会加剧蒸腾与热害，影响结实与灌浆",
            action="检查田间水层，必要时灌深水降温或调整作业时段",
        ),
        ThresholdRule(
            "env_risk.cold",
            StandardMetric.AIR_TEMP.value,
            "below",
            default_threshold=settings.BROADCAST_TEMP_LOW,
            fact_template="气温降至 {value:.1f}{unit}，低于低温阈值 {threshold:.1f}{unit}",
            impact="低温会造成冷害，幼苗期与花期尤其敏感",
            action="采取覆盖、灌水等保温措施，暂停易受冻的田间作业",
        ),
        ThresholdRule(
            "env_risk.soil_dry",
            StandardMetric.SOIL_MOISTURE.value,
            "below",
            default_threshold=settings.BROADCAST_SOIL_DRY,
            fact_template="土壤相对含水量 {value:.1f}{unit}，低于干旱阈值 {threshold:.1f}{unit}",
            impact="土壤缺墒会抑制根系吸收，影响正常生长",
            action="及时灌溉补水，灌溉后复查墒情",
        ),
        ThresholdRule(
            "env_risk.humidity",
            StandardMetric.AIR_HUMIDITY.value,
            "above",
            default_threshold=settings.BROADCAST_HUMIDITY_HIGH,
            fact_template="空气相对湿度 {value:.1f}{unit}，超过高湿阈值 {threshold:.1f}{unit}",
            impact="高湿环境利于病害流行，通风不良地块风险更高",
            action="加强通风排湿，注意巡查病斑并及时防治",
        ),
        ThresholdRule(
            "env_risk.rain",
            StandardMetric.RAINFALL.value,
            "above",
            default_threshold=settings.BROADCAST_RAINFALL_MM,
            fact_template="降雨量达到 {value:.1f}{unit}，超过阈值 {threshold:.1f}{unit}",
            impact="强降雨易造成积水与冲刷，影响根系与肥料有效性",
            action="及时清沟排水，雨后注意病害防治",
        ),
        ThresholdRule(
            "env_risk.wind",
            StandardMetric.WIND_SPEED.value,
            "above",
            default_threshold=settings.BROADCAST_WIND_MS,
            fact_template="风速达到 {value:.1f}{unit}，超过大风阈值 {threshold:.1f}{unit}",
            impact="大风易造成倒伏与机械损伤，喷药作业也会漂移失效",
            action="暂停喷药与高秆作业，加固易倒伏地块",
        ),
    ]


__all__ = [
    "SOURCE_TIMELINESS",
    "ThresholdRule",
    "callout_of",
    "default_threshold_rules",
    "resolve_threshold",
    "unit_of",
]
