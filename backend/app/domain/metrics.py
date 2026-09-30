"""标准环境指标与新鲜度窗口的唯一定义处。

两类常量都放这里，理由是它们被多处共用，任何一处硬编码都会在调整时漏改：

- `StandardMetric` 与 `METRIC_RANGES`：`field_mapping.standard_metric`、
  `env_reading.metric` 与解析器三处出现，取值漂移会让读数写进去却查不出来；
  合法区间同时是 T5-07 `classify_quality` 判定 `suspect` 的判据。
- `FRESH_WINDOW_MINUTES` / `REFERENCE_WINDOW_HOURS`：环境事实三级回退（T3-02）与
  读数质量标记（T5-07）共用的新鲜度窗口。30 分钟为"实时"、24 小时为"参考"，
  取值来自产品说明书 4.4.3 功能点 3。
"""

from enum import Enum


class StandardMetric(str, Enum):
    """系统标准环境指标。解析器把设备原始字段映射到枚举上，落不到枚举即映射非法。"""

    SOIL_TEMP = "soil_temp"
    SOIL_MOISTURE = "soil_moisture"
    AIR_TEMP = "air_temp"
    AIR_HUMIDITY = "air_humidity"
    RAINFALL = "rainfall"
    LIGHT_LUX = "light_lux"
    WIND_SPEED = "wind_speed"
    SOIL_PH = "soil_ph"
    SOIL_EC = "soil_ec"


# 各指标默认单位（展示用）。
DEFAULT_UNITS: dict[StandardMetric, str] = {
    StandardMetric.SOIL_TEMP: "°C",
    StandardMetric.SOIL_MOISTURE: "%",
    StandardMetric.AIR_TEMP: "°C",
    StandardMetric.AIR_HUMIDITY: "%",
    StandardMetric.RAINFALL: "mm",
    StandardMetric.LIGHT_LUX: "lux",
    StandardMetric.WIND_SPEED: "m/s",
    StandardMetric.SOIL_PH: "",
    StandardMetric.SOIL_EC: "mS/cm",
}

# 各指标合法物理量程（闭区间）。超出即 `suspect`：保留原始值供排查，不参与阈值判断。
METRIC_RANGES: dict[StandardMetric, tuple[float, float]] = {
    StandardMetric.SOIL_TEMP: (-10.0, 60.0),
    StandardMetric.SOIL_MOISTURE: (0.0, 100.0),
    StandardMetric.AIR_TEMP: (-40.0, 60.0),
    StandardMetric.AIR_HUMIDITY: (0.0, 100.0),
    StandardMetric.RAINFALL: (0.0, 500.0),
    StandardMetric.LIGHT_LUX: (0.0, 200000.0),
    StandardMetric.WIND_SPEED: (0.0, 60.0),
    StandardMetric.SOIL_PH: (0.0, 14.0),
    StandardMetric.SOIL_EC: (0.0, 20.0),
}

# 实时新鲜窗口：读数在该窗口内且 `quality=ok` 时判为 `source="sensor"`。
FRESH_WINDOW_MINUTES = 30
# 参考窗口：超过实时窗口但在该窗口内，判为 `sensor_stale`（可给弱提示，须标注时效）。
REFERENCE_WINDOW_HOURS = 24


__all__ = [
    "DEFAULT_UNITS",
    "FRESH_WINDOW_MINUTES",
    "METRIC_RANGES",
    "REFERENCE_WINDOW_HOURS",
    "StandardMetric",
]
