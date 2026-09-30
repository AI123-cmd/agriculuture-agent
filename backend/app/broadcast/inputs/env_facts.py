"""环境事实三级回退采集（T3-02，技术方案 11.2）。

四级结果由三级回退产出：自有传感器新鲜读数 → 传感器陈旧读数（带时效标注）→
天气服务 → 均不可用。任何一级都不得用推测值填充，`source="none"` 时环境风险类
提示条目在生成阶段即被拦截，不进入文案环节（产品说明书 6.4 红线四）。

`EnvFacts.source` 的四个取值与 T3-01 的 `prompt_snapshot.env_source`、T2-33 的
`STALE_ENV_SOURCES = {"none", "sensor_stale"}` 完全一致，不得新增第五个取值。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.domain.metrics import FRESH_WINDOW_MINUTES, REFERENCE_WINDOW_HOURS
from app.tools.sensors import sensor_service
from app.tools.weather import weather_service


class EnvFacts(BaseModel):
    """环境事实：三级回退后的扁平指标映射与来源标注。"""

    model_config = ConfigDict(extra="forbid")

    source: Literal["sensor", "sensor_stale", "weather", "none"]
    values: dict[str, float] = {}
    observed_at: datetime | None = None
    staleness_note: str | None = None
    # 地区回退实际命中级，供界面标注"当前按 XX 市通用数据"。
    region_hit_level: str | None = None
    # 指标 -> 来源标识，进 prompt_item.facts_provenance 逐指标登记来源。
    provenance: dict[str, str] = {}


async def collect_env_facts(user_id: str, plot_id: str | None) -> EnvFacts:
    """环境事实采集：三级回退，不可用即返回 empty（该路提示不生成）。"""
    # 优先级 1：自有传感器
    reading = await sensor_service.latest(user_id, plot_id)
    if reading and reading.is_fresh(minutes=FRESH_WINDOW_MINUTES):
        return EnvFacts(
            source="sensor",
            values=reading.values,
            observed_at=reading.observed_at,
            provenance={metric: "sensor" for metric in reading.values},
        )
    if reading and reading.is_within_hours(REFERENCE_WINDOW_HOURS):
        # 参考值：可用但必须标注"数据为 XX 小时前"
        return EnvFacts(
            source="sensor_stale",
            values=reading.values,
            observed_at=reading.observed_at,
            staleness_note=f"数据为 {reading.age_hours()} 小时前",
            provenance={metric: "sensor" for metric in reading.values},
        )

    # 优先级 2：天气服务
    weather = await weather_service.current(user_id)
    if weather:
        return EnvFacts(
            source="weather",
            values=weather.values,
            observed_at=weather.observed_at,
            region_hit_level=weather.region_hit_level,
            provenance={metric: "weather" for metric in weather.values},
        )

    # 优先级 3：均不可用 → 不生成环境风险类提示
    return EnvFacts(source="none", values={}, observed_at=None)


ENV_RISK_KINDS = {"env_risk"}


def is_env_risk_blocked(facts: EnvFacts, source_kind: str) -> bool:
    """环境事实不可用时，环境风险类条目在生成阶段被拦截。"""
    return facts.source == "none" and source_kind in ENV_RISK_KINDS


__all__ = ["ENV_RISK_KINDS", "EnvFacts", "collect_env_facts", "is_env_risk_blocked"]
