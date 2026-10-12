"""传感器读取工具（T3-02）：真实读数读取 + 三级取数入口。

三处关键契约：

- `Reading` 是读数的值对象，携带 `values`（指标 -> 数值）、`observed_at` 与
  `quality`。`is_fresh()` / `is_within_hours()` / `age_hours()` 的窗口常量来自
  `app/domain/metrics.py`，与环境看板、读数质量标记共用同一份定义。
- `is_fresh()` 内部叠加 `quality == "ok"`：被标 `suspect` 的读数等同于该级缺失，
  直接降级到下一级，而不是带着可疑数字进入规则层。
- `query_sensors()` 三级取数：真实读数 → 天气 → 静态兜底。静态兜底的 `source`
  明确标为 `"fallback"`，不再伪装为传感器读数（不再出现 `"sensor_snapshot"`）。

不出现 `db.query`（走仓储层），符合全局约定 L4。
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SessionLocal
from app.domain.metrics import FRESH_WINDOW_MINUTES, REFERENCE_WINDOW_HOURS
from app.models import Device, EnvReading
from app.repositories import Repositories, env_reading_repo
from app.tools.weather import weather_service

# StandardMetric 取值 -> 旧看板消费的键名。EnvironmentService 仍读这些键，
# 收敛到一处避免两套命名在多个消费者之间漂移。
_LEGACY_KEYS: dict[str, str] = {
    "air_temp": "air_temperature",
    "soil_temp": "soil_temperature",
}

# 无任何可用读数时的静态兜底值。仅用于零设备农户的页面内容，`source="fallback"`
# 使其可识别为兜底值而非实测（产品说明书 6.4 红线四）。
_FALLBACK_VALUES: dict[str, float] = {
    "air_temperature": 26.5,
    "air_humidity": 78.0,
    "soil_temperature": 25.0,
    "soil_moisture": 23.0,
    "soil_ph": 6.6,
    "soil_ec": 1.2,
    "light_lux": 58000.0,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """裸时间戳按 UTC 处理：数据库列带时区，但离线构造的值对象可能没有。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Reading:
    """一批读数（同一 `observed_at` 下的多指标）与新鲜度判定。"""

    def __init__(self, values: dict[str, float], observed_at: datetime, quality: str = "ok") -> None:
        self.values = values
        self.observed_at = observed_at
        self.quality = quality

    def age_hours(self) -> int:
        """距现在几小时（向下取整，负值归零）。TT5-08 的时效标注共用它。"""
        age = _now() - _as_aware(self.observed_at)
        return max(int(age.total_seconds() // 3600), 0)

    def is_fresh(self, minutes: int = FRESH_WINDOW_MINUTES) -> bool:
        """新鲜 且 `quality=ok`。`suspect`/`stale` 一律为假，直接降级到下一级。"""
        if self.quality != "ok":
            return False
        age = _now() - _as_aware(self.observed_at)
        return timedelta(0) <= age <= timedelta(minutes=minutes)

    def is_within_hours(self, hours: int = REFERENCE_WINDOW_HOURS) -> bool:
        """在参考窗口内（可给弱提示，须标注时效）。`suspect` 不参与，视为该级缺失。"""
        if self.quality == "suspect":
            return False
        age = _now() - _as_aware(self.observed_at)
        return timedelta(0) <= age <= timedelta(hours=hours)


def _batch_quality(rows: Sequence[EnvReading]) -> str:
    """批次质量：suspect 优先于 stale 优先于 ok（最保守者胜出）。"""
    qualities = {row.quality for row in rows}
    for candidate in ("suspect", "stale", "ok"):
        if candidate in qualities:
            return candidate
    return "ok"


class SensorService:
    """自有传感器读数读取。

    `latest()` 自带会话工厂：`collect_env_facts` 的签名（T3-02 明文）不带会话，
    因此这里不接收 `AsyncSession`，由内部开短事务读取后立刻归还连接。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory or SessionLocal

    async def latest(self, user_id: str, plot_id: str | None) -> Reading | None:
        """取最新一批读数；无读数返回 `None`（绝不构造推测值）。"""
        async with self._session_factory() as db:
            return await self.latest_with(db, user_id, plot_id)

    async def latest_with(self, db: AsyncSession, user_id: str, plot_id: str | None) -> Reading | None:
        """带会话版本：供已有会话的调用方（看板、播报）复用同一连接。"""
        rows = await env_reading_repo.latest_batch(db, user_id, farm_entry_id=plot_id)
        if not rows:
            return None
        # 同一批次共享 observed_at，quality 取最保守者：任一条不合格即整批按该质量处理。
        return Reading(values=_to_values(rows), observed_at=rows[0].observed_at, quality=_batch_quality(rows))


def _to_values(rows: Sequence[EnvReading]) -> dict[str, float]:
    return {row.metric: float(row.value) for row in rows}


sensor_service = SensorService()


async def query_sensors(repos: Repositories | None, user_id: str) -> dict:
    """读农户名下传感器快照：真实读数 → 天气服务 → 静态兜底（T5-07）。

    `source` 三级分别为 `sensor` / `weather` / `fallback`。静态兜底只在真实读数与天气都不可用时
    才出现，且 `source="fallback"` 使其可识别为兜底值而非实测（产品说明书 6.4 红线四禁止"无数据
    支撑的数字"，故不回退为伪装实测，只回退为可识别兜底）。返回值保留旧看板消费的键名，真实读数
    优先，缺项用兜底值补齐。
    """
    devices: Sequence[Device] = [] if repos is None else await repos.device.list_for_user(repos.db, user_id)

    values: dict[str, float] = {}
    source = "fallback"
    if repos is not None:
        reading = await sensor_service.latest_with(repos.db, user_id, None)
        if reading is not None and reading.is_within_hours():
            values = {_LEGACY_KEYS.get(k, k): v for k, v in reading.values.items()}
            source = "sensor"
        else:
            weather = await weather_service.current_with(repos.db, user_id)
            if weather is not None and weather.values:
                values = {_LEGACY_KEYS.get(k, k): v for k, v in weather.values.items()}
                source = "weather"

    return {
        **_FALLBACK_VALUES,
        **values,
        "devices": [
            {"id": d.id, "type": d.device_type, "name": d.device_name or d.device_sn, "status": d.status}
            for d in devices
        ],
        "source": source,
    }


__all__ = ["Reading", "SensorService", "query_sensors", "sensor_service"]
