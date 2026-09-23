"""环境快照：统一收敛设备/天气/画像数据源，遮蔽差异。"""

from dataclasses import dataclass, field

from app.repositories import Repositories
from app.tools import sensors, weather


@dataclass
class EnvSnapshot:
    """规则引擎的统一输入快照，字段可为 None 表示缺失。"""

    temp: float | None = None  # 空气温度 ℃
    humidity: float | None = None  # 空气湿度 %
    soil_moisture: float | None = None  # 土壤含水率 %
    soil_temp: float | None = None  # 土壤温度 ℃
    light: float | None = None  # 光照 lux
    rainfall: float | None = None  # 降雨 mm（实况/预报）
    wind_speed: float | None = None  # 风速 m/s
    crop_name: str = ""
    growth_stage: str = ""
    location: str = ""
    forecast: dict = field(default_factory=dict)
    source: str = "mock"  # device / weather / mock


async def build_snapshot(repos: Repositories | None, user_id: str) -> EnvSnapshot:
    """组装环境快照：真实设备读数 → 天气兜底 → mock 兜底。

    Device 表当前尚无实时读数字段，故传感器值由 tools.sensors 提供
    （有设备则附设备列表、无设备时静态兜底），接入真实平台后在 sensors 处替换。
    """
    sensor = await sensors.query_sensors(repos, user_id) if repos is not None else {}

    crop = None
    profile_row = None
    if repos is not None:
        crop = await repos.crop.main_crop(repos.db, user_id)
        profile_row = await repos.profile.get_for_user(repos.db, user_id)

    location = ""
    if profile_row:
        location = f"{profile_row.province or ''}{profile_row.city or ''}{profile_row.district or ''}"

    weather_info = weather.query_weather(location)
    summary = weather_info.get("summary", "")

    return EnvSnapshot(
        temp=sensor.get("air_temperature"),
        humidity=sensor.get("air_humidity"),
        soil_moisture=sensor.get("soil_moisture"),
        soil_temp=sensor.get("soil_temperature"),
        light=sensor.get("light_lux"),
        crop_name=crop.crop_name if crop else "",
        growth_stage=crop.growth_stage or "" if crop else "",
        location=location,
        forecast={"summary": summary, "rainfall": None, "condition": summary},
        source=sensor.get("source", "mock"),
    )


__all__ = ["EnvSnapshot", "build_snapshot"]
