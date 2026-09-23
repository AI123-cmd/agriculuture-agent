"""阈值规则集：数据驱动，命中即产出播报提醒。"""

from typing import TYPE_CHECKING

from .base import BaseRule, RuleResult, register

if TYPE_CHECKING:
    from app.broadcast.snapshot import EnvSnapshot
    from app.core.config import Settings


@register
class HighTempRule(BaseRule):
    """高温热害风险。"""

    name = "high_temp"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        if snapshot.temp is None or snapshot.temp < settings.BROADCAST_TEMP_HIGH:
            return None
        return RuleResult(
            type="high_temp",
            level="warning",
            title="高温热害风险",
            content=f"当前气温 {snapshot.temp:.0f}℃，已达高温阈值，高温时段易发生热害胁迫。",
            source=self._source(snapshot),
            action="避开正午高温作业，适时灌溉降低田间温度",
        )

    @staticmethod
    def _source(snapshot: "EnvSnapshot") -> str:
        return "weather" if snapshot.source == "mock" else "sensor"


@register
class LowTempRule(BaseRule):
    """低温/冻害风险。"""

    name = "low_temp"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        if snapshot.temp is None or snapshot.temp > settings.BROADCAST_TEMP_LOW:
            return None
        return RuleResult(
            type="low_temp",
            level="warning",
            title="低温冻害风险",
            content=f"当前气温 {snapshot.temp:.0f}℃，已达低温阈值，请注意防寒防冻。",
            source=self._source(snapshot),
            action="覆盖保温或灌水防冻，减少冻害",
        )

    @staticmethod
    def _source(snapshot: "EnvSnapshot") -> str:
        return "weather" if snapshot.source == "mock" else "sensor"


@register
class SoilDryRule(BaseRule):
    """土壤干旱风险。"""

    name = "soil_dry"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        if snapshot.soil_moisture is None or snapshot.soil_moisture >= settings.BROADCAST_SOIL_DRY:
            return None
        return RuleResult(
            type="soil_dry",
            level="warning",
            title="土壤墒情不足",
            content=f"当前土壤含水率 {snapshot.soil_moisture:.0f}%，低于阈值，需检查墒情并及时补水。",
            source="sensor",
            action="及时灌溉，避免作物受旱",
        )


@register
class HighHumidityRule(BaseRule):
    """高湿风险。"""

    name = "high_humidity"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        if snapshot.humidity is None or snapshot.humidity < settings.BROADCAST_HUMIDITY_HIGH:
            return None
        return RuleResult(
            type="high_humidity",
            level="warning",
            title="湿度偏高、易发病",
            content=f"当前空气湿度 {snapshot.humidity:.0f}%，湿度偏高易诱发病害，注意通风排湿。",
            source="sensor",
            action="加强通风排湿，降低田间湿度",
        )


@register
class RainfallRule(BaseRule):
    """降雨提醒。"""

    name = "rainfall"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        rainfall = snapshot.forecast.get("rainfall") if snapshot.forecast else None
        if rainfall is None or rainfall <= settings.BROADCAST_RAINFALL_MM:
            return None
        return RuleResult(
            type="rainfall",
            level="warning",
            title="降雨预警",
            content=f"预报降雨 {rainfall:.0f}mm，请提前清沟排水，防范渍涝。",
            source="weather",
            action="提前清沟排水，防范渍涝",
        )


@register
class StrongWindRule(BaseRule):
    """大风提醒。"""

    name = "strong_wind"

    def match(self, snapshot: "EnvSnapshot", settings: "Settings") -> RuleResult | None:
        if snapshot.wind_speed is None or snapshot.wind_speed < settings.BROADCAST_WIND_MS:
            return None
        return RuleResult(
            type="strong_wind",
            level="warning",
            title="大风预警",
            content=f"当前风速 {snapshot.wind_speed:.0f} m/s，注意倒伏与飞防作业安全。",
            source="weather",
            action="暂停飞防作业，加固设施防倒伏",
        )
