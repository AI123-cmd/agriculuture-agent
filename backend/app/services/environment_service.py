"""环境监测服务（T5-08）：读数数据源统一走环境事实采集，并标注来源与新鲜度。

**数据源收敛到 `collect_env_facts`**（`app/broadcast/inputs/env_facts.py`）：看板、提示快照
（T3-08）与问诊接口（T2-33）三处消费的是同一份环境事实，取数、来源集合（`sensor` /
`sensor_stale` / `weather` / `none`）与新鲜度窗口（30 分钟 / 24 小时）都只有一处实现。
若本服务自己写一遍"查最新读数、判断时间"，T5-07 调整窗口时就会漏改，而漏改不报错，只让两处
判断悄悄分叉——同一份读数在首页说"本地传感器"、在对话页说"暂无环境数据"。

三条与产品说明书的对应：

1. **来源标注**（4.4.2）：农户必须能看出数字来自本地传感器、公共天气还是暂无数据，
   以及数据是不是几小时前的。文案取自 `SOURCE_LABELS`（`app/domain/metrics.py`），不在此拼写。
2. **不出现无依据的数字**（6.4 红线四）：读数缺失时给 `None` 与"—"，不再返回硬编码兜底值
   （旧实现从 `tools/sensors.py` 的静态常量取数，那些数字伪装成本地实测，前端无从分辨）。
3. **状态标注**（4.4.3 功能点 4）：每个指标给"正常/偏高/偏低"。判据用与提示系统同一套阈值，
   从 `core/config.py` 读取，避免"看板说正常、提示说高温"。

分层：本服务不出现裸 ORM 查询（守全局约定 L4）。设备段一律走 `device_service.list_devices()`，
与环境详情、设备管理列表共用同一套在线判据，避免"环境页说在线、设备页说离线"（T5-09 第六条）。
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast.inputs.env_facts import EnvFacts, collect_env_facts
from app.core.config import get_settings
from app.domain.metrics import DEFAULT_UNITS, METRIC_LABELS, SOURCE_LABELS, STANDARD_METRICS, StandardMetric
from app.schemas.device import DeviceListItem
from app.schemas.environment import BoardMetric, EnvironmentBoard, SourceBadge
from app.services.device_service import list_devices
from app.services.env_service import in_valid_range
from app.tools.sensors import age_hours

# 状态判据用与提示系统同一套阈值（`BROADCAST_` 六项），配置项名而非数值写在此表里，
# 数值仍从 `core/config.py` 读取——阈值调整只需改配置一处。
# 结构：指标 -> (上限配置名 | None, 下限配置名 | None)；两者都没有的指标只判"有读数即正常"。
_THRESHOLD_KEYS: dict[str, tuple[str | None, str | None]] = {
    StandardMetric.AIR_TEMP.value: ("BROADCAST_TEMP_HIGH", "BROADCAST_TEMP_LOW"),
    StandardMetric.SOIL_MOISTURE.value: (None, "BROADCAST_SOIL_DRY"),
    StandardMetric.AIR_HUMIDITY.value: ("BROADCAST_HUMIDITY_HIGH", None),
    StandardMetric.RAINFALL.value: ("BROADCAST_RAINFALL_MM", None),
    StandardMetric.WIND_SPEED.value: ("BROADCAST_WIND_MS", None),
}

# 无数据时的展示占位符。刻意不用 "0" 或任何数字：0 会被读成"实测为零"，
# 而这里表达的是"没有读数"（产品说明书 6.4 红线四）。
_MISSING = "—"


def _freshness_note(observed_at: datetime | None, source: str) -> str | None:
    """仅 `sensor_stale` 带时效标注；`weather` 不作时效标注。

    小时数向下取整并设下限 1：不足 1 小时显示"1 小时前"而不是"0 小时前"（后者语义上等于
    实时，与 `sensor_stale` 的定义矛盾）。超过 24 小时的读数不会走到这里（`collect_env_facts`
    已降级到天气），因此小时数上限是 24。

    小时数由 `tools/sensors.py::age_hours` 计算——两处都算"距现在几小时"，共用同一实现才不会
    因时区处理不一致而在两处得出不同小时数。
    """
    if source != "sensor_stale" or observed_at is None:
        return None
    hours = age_hours(observed_at)
    if hours < 1:
        return "数据为 1 小时前"
    return f"数据为 {int(hours)} 小时前"


def _badge_of(facts: EnvFacts) -> SourceBadge:
    """由环境事实生成来源标注。地区命中级仅天气来源时给出。"""
    return SourceBadge(
        source=facts.source,
        label=SOURCE_LABELS[facts.source],
        freshnessNote=_freshness_note(facts.observed_at, facts.source),
        regionHitLevel=facts.region_hit_level if facts.source == "weather" else None,
    )


def _metric_quality(metric: str, value: float | None, source: str) -> str:
    """指标质量三值（与 T5-07 的 `ok` / `suspect` / `stale` 同口径）。

    判据复用 `env_service.in_valid_range`：超量程即 `suspect`（坏数据），缺失或来源陈旧即
    `stale`，其余为 `ok`。这里重判一次而非直接采信 `env_reading.quality`，因为看板的数值
    可能来自天气服务（无对应读数行）；对同一份判据各算一次，比"读不到行就给 ok"更保守。
    """
    if value is None:
        return "stale"
    if not in_valid_range(metric, value):
        return "suspect"
    if source == "sensor_stale":
        return "stale"
    return "ok"


def _metric_status(metric: str, value: float | None, quality: str) -> str:
    """状态标注：`normal` / `high` / `low` / `unknown`。

    `quality != "ok"` 一律 `unknown` 而不是按数值算状态：一条 30 小时前的 22°C 不能标"正常"
    （不代表现在），一条超量程的 265°C 不能标"偏高"（是坏数据不是高温）。展示仍给原始数值，
    但状态列不参与判断——这与 T3-02"suspect 等同于该级缺失"的口径一致。
    """
    if value is None or quality != "ok":
        return "unknown"
    high_key, low_key = _THRESHOLD_KEYS.get(metric, (None, None))
    settings = get_settings()
    if high_key is not None and value > float(getattr(settings, high_key)):
        return "high"
    if low_key is not None and value < float(getattr(settings, low_key)):
        return "low"
    return "normal"


def _to_board_metric(metric: StandardMetric, facts: EnvFacts) -> BoardMetric:
    """把环境事实里的一个标准指标装配成看板行。"""
    value = facts.values.get(metric.value)
    quality = _metric_quality(metric.value, value, facts.source)
    return BoardMetric(
        metric=metric.value,
        label=METRIC_LABELS[metric],
        value=value,
        unit=DEFAULT_UNITS[metric],
        status=_metric_status(metric.value, value, quality),
        quality=quality,
        observedAt=facts.observed_at if value is not None else None,
    )


def _fmt(value: float | None, unit: str = "") -> str:
    """数值展示：缺失给占位符，不构造"0"这类会被误读为实测的值。"""
    if value is None:
        return _MISSING
    return f"{value:g}{unit}"


class EnvironmentService:
    async def board(self, db: AsyncSession, user_id: str, plot_id: str | None = None) -> EnvironmentBoard:
        """环境看板：与提示快照、问诊共享同一份环境事实，避免可信度结论分叉。"""
        facts = await collect_env_facts(user_id, plot_id)
        metrics = [_to_board_metric(metric, facts) for metric in STANDARD_METRICS]
        return EnvironmentBoard(
            metrics=metrics,
            sourceBadge=_badge_of(facts),
            devices=await self._devices(db, user_id),
            lastUpdateAt=facts.observed_at,
        )

    async def summary(self, db: AsyncSession, user_id: str) -> dict:
        """首页环境摘要：返回结构保持原样，内部数据源改为环境事实采集。"""
        facts = await collect_env_facts(user_id, None)
        values = facts.values
        soil_temp = values.get(StandardMetric.SOIL_TEMP.value)
        soil_moisture = values.get(StandardMetric.SOIL_MOISTURE.value)
        soil_ph = values.get(StandardMetric.SOIL_PH.value)
        light_lux = values.get(StandardMetric.LIGHT_LUX.value)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return {
            "temperature": values.get(StandardMetric.AIR_TEMP.value),
            "humidity": values.get(StandardMetric.AIR_HUMIDITY.value),
            "soilStatus": self._soil_status(soil_moisture),
            "soilDetail": self._join(
                f"土壤温度{_fmt(soil_temp, '℃')}",
                f"含水率{_fmt(soil_moisture, '%')}",
                f"pH {_fmt(soil_ph)}",
            ),
            "lightStatus": self._light_status(light_lux),
            "lightDetail": f"光照强度{_fmt(light_lux, ' lux')}",
            "lastUpdateAt": now,
            "sourceBadge": _badge_of(facts).model_dump(mode="json"),
        }

    async def detail(self, db: AsyncSession, user_id: str) -> dict:
        """环境详情：分组的键值列表，数据源同样改为环境事实采集。

        设备段改为复用 `list_devices()`（T5-09 第六条）：与环境详情、设备管理列表共用同一套
        在线判据，两个界面不会对同一台设备给出不同的在线状态。
        """
        facts = await collect_env_facts(user_id, None)
        values = facts.values
        devices = await self._devices(db, user_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        air_temp = values.get(StandardMetric.AIR_TEMP.value)
        air_humidity = values.get(StandardMetric.AIR_HUMIDITY.value)
        soil_temp = values.get(StandardMetric.SOIL_TEMP.value)
        soil_moisture = values.get(StandardMetric.SOIL_MOISTURE.value)
        soil_ph = values.get(StandardMetric.SOIL_PH.value)
        soil_ec = values.get(StandardMetric.SOIL_EC.value)
        light_lux = values.get(StandardMetric.LIGHT_LUX.value)
        wind_speed = values.get(StandardMetric.WIND_SPEED.value)
        rainfall = values.get(StandardMetric.RAINFALL.value)
        return {
            "metrics": [
                {"id": "temp", "label": "气温", "value": _fmt(air_temp), "unit": "°C"},
                {"id": "humidity", "label": "湿度", "value": _fmt(air_humidity), "unit": "%"},
                {"id": "soil", "label": "土壤湿度", "value": _fmt(soil_moisture), "unit": "%"},
                {"id": "light", "label": "光照", "value": _fmt(light_lux), "unit": "lux"},
            ],
            "airData": [
                {"label": "空气温度", "value": _fmt(air_temp, "°C")},
                {"label": "空气湿度", "value": _fmt(air_humidity, "%")},
                {"label": "风速", "value": _fmt(wind_speed, " m/s")},
            ],
            "soilData": [
                {"label": "土壤温度", "value": _fmt(soil_temp, "°C")},
                {"label": "土壤含水率", "value": _fmt(soil_moisture, "%")},
                {"label": "土壤pH值", "value": _fmt(soil_ph)},
                {"label": "电导率(EC)", "value": _fmt(soil_ec, " mS/cm")},
            ],
            "weatherData": [
                {"label": "光照强度", "value": _fmt(light_lux, " lux")},
                {"label": "降雨量", "value": _fmt(rainfall, " mm")},
            ],
            "devices": [item.model_dump(mode="json", by_alias=True) for item in devices],
            "lastUpdateAt": now,
            "sourceBadge": _badge_of(facts).model_dump(mode="json"),
        }

    async def _devices(self, db: AsyncSession, user_id: str) -> list[DeviceListItem]:
        """设备段：复用设备管理列表的同一实现（T5-09 第六条）。

        不再直接查旧 `Device` 表：绑定/上报/解绑都写 `device_config`，两个界面共用同一套在线
        判据，才不会出现"环境页说在线、设备页说离线"。返回项含 `deviceSn` / `onlineState`，
        与 `GET /api/device/list` 逐字段一致。
        """
        return (await list_devices(db, user_id)).devices

    @staticmethod
    def _join(*parts: str) -> str:
        """拼接详情文本：过滤掉缺失项（值为占位符的部分），不留"pH —"这类空洞。"""
        return "，".join(part for part in parts if not part.endswith(_MISSING))

    @staticmethod
    def _soil_status(moisture: float | None) -> str:
        if moisture is None:
            return "未知"
        if moisture < get_settings().BROADCAST_SOIL_DRY:
            return "偏干"
        return "适宜"

    @staticmethod
    def _light_status(light_lux: float | None) -> str:
        if light_lux is None:
            return "未知"
        return "良好" if light_lux >= 30000 else "一般"


__all__ = ["EnvironmentService"]
