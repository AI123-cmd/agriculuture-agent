"""天气工具（T3-02 / T5-08）：`current()` 返回结构化当前天气，`query_weather` 供 MCP 目录用。

`current(user_id)` 从农户档案的省/市/县推导查询地区，并把"实际命中级"
（county / city / province）带回来供界面标注（全局约定六）。任何不可用情况
（无档案地区、HTTP 超时、配额耗尽、无覆盖）一律返回 `None`，绝不用推测值填充。
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SessionLocal
from app.integrations.weather import fetch_current, get_weather_summary
from app.models import FarmerProfile
from app.repositories import profile_repo


@dataclass
class WeatherFacts:
    """天气事实：扁平指标映射 + 观测时间 + 实际命中地区级别。"""

    values: dict[str, float] = field(default_factory=dict)
    observed_at: datetime | None = None
    region_hit_level: str | None = None


def _region_of(profile: FarmerProfile | None) -> tuple[str, str | None]:
    """由档案字段推导查询地区与命中级。缺地区返回空串（调用方据此归为不可用）。"""
    if profile is None:
        return "", None
    province = (profile.province or "").strip()
    city = (profile.city or "").strip()
    district = (profile.district or "").strip()
    if district:
        return f"{province}{city}{district}", "county"
    if city:
        return f"{province}{city}", "city"
    if province:
        return province, "province"
    return "", None


class WeatherService:
    """天气服务：`current()` 自带会话工厂，签名不带 `AsyncSession`（T3-02 明文）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory or SessionLocal

    async def current(self, user_id: str) -> WeatherFacts | None:
        async with self._session_factory() as db:
            return await self.current_with(db, user_id)

    async def current_with(self, db: AsyncSession, user_id: str) -> WeatherFacts | None:
        """带会话版本：供已有会话的调用方复用同一连接。"""
        profile = await profile_repo.get_for_user(db, user_id)
        region, hit_level = _region_of(profile)
        if not region:
            # 档案缺地区：天气服务无法定位，归为不可用（T3-02 第三节）。
            return None
        data = fetch_current(region)
        if data is None:
            return None
        return WeatherFacts(values=data["values"], observed_at=data["observed_at"], region_hit_level=hit_level)


weather_service = WeatherService()


def query_weather(location: str = "") -> dict:
    """MCP 目录工具：查询天气摘要文本，底层走 `app/integrations/weather.py`。"""
    return {
        "location": location,
        "summary": get_weather_summary(location),
        "source": "weather",
    }


__all__ = ["WeatherFacts", "WeatherService", "query_weather", "weather_service"]
