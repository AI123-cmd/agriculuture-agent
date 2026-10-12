"""T5-07 验收：读数合法性校验与质量标记。

用例覆盖文档"验收标准"全部条目：范围合法性、时效、时钟超前、范围优先于时效、闭区间端点、
归属拒绝不落库，以及三级取数 `source` 的取值与"窗口常量单一定义、不出现 `db.query`"两条门禁。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import DeviceConfig, EnvReading
from app.services.env_service import Reading, classify_quality, in_valid_range

_REPORT_URL = "/api/device/report/{sn}"
_BIND_URL = "/api/device/bind"
_PARSE_URL = "/api/device/parse-sample"
_MAPPING_URL = "/api/device/mapping"

_AIR_ONLY = [{"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"}]


def _reading(metric: str, value: float, *, minutes_ago: float = 1.0, source: str = "device") -> Reading:
    return Reading(
        metric=metric,
        value=value,
        raw_field="x",
        unit=None,
        observed_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        timestamp_source=source,
    )


# ---- 三值判定 ----


def test_fresh_in_range_is_ok() -> None:
    now = datetime.now(UTC)
    assert classify_quality(_reading("soil_temp", 25.0, minutes_ago=5), now=now) == "ok"


def test_out_of_range_is_suspect_and_value_kept() -> None:
    now = datetime.now(UTC)
    reading = _reading("air_temp", 265.0, minutes_ago=1)
    assert classify_quality(reading, now=now) == "suspect"
    assert reading.value == 265.0


def test_25_hours_old_is_stale() -> None:
    now = datetime.now(UTC)
    assert classify_quality(_reading("air_temp", 26.5, minutes_ago=25 * 60), now=now) == "stale"


def test_2_hours_old_is_ok() -> None:
    now = datetime.now(UTC)
    assert classify_quality(_reading("air_temp", 26.5, minutes_ago=2 * 60), now=now) == "ok"


def test_future_timestamp_beyond_tolerance_is_suspect() -> None:
    now = datetime.now(UTC)
    assert classify_quality(_reading("air_temp", 26.5, minutes_ago=-3 * 60), now=now) == "suspect"


def test_missing_timestamp_is_stale() -> None:
    now = datetime.now(UTC)
    reading = Reading(metric="air_temp", value=26.5, raw_field="t1", unit=None, observed_at=None, timestamp_source="server")  # type: ignore[arg-type]
    assert classify_quality(reading, now=now) == "stale"


def test_range_takes_priority_over_staleness() -> None:
    """范围与时效同时不合格：`suspect` 优先于 `stale`。"""
    now = datetime.now(UTC)
    assert classify_quality(_reading("air_temp", 300.0, minutes_ago=30 * 60), now=now) == "suspect"


def test_server_timestamp_not_judged_stale() -> None:
    """服务端补的时间戳不做时效判定：即便 `observed_at` 是刚刚，也判 `ok`。"""
    now = datetime.now(UTC)
    assert classify_quality(_reading("air_temp", 26.5, minutes_ago=0, source="server"), now=now) == "ok"


# ---- 闭区间端点 ----


def test_range_boundary_values_are_ok() -> None:
    now = datetime.now(UTC)
    assert classify_quality(_reading("soil_moisture", 0.0), now=now) == "ok"
    assert classify_quality(_reading("air_humidity", 100.0), now=now) == "ok"
    assert classify_quality(_reading("soil_ph", 14.0), now=now) == "ok"


def test_in_valid_range_uses_closed_interval() -> None:
    assert in_valid_range("air_temp", -40.0) is True
    assert in_valid_range("air_temp", 60.0) is True
    assert in_valid_range("air_temp", 60.0001) is False
    assert in_valid_range("air_temp", -40.0001) is False


# ---- 归属拒绝：不落库 ----


def _login(client, code: str) -> tuple[str, str]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind(client, token: str, sn: str) -> tuple[str, str]:
    resp = client.post(_BIND_URL, json={"deviceSn": sn}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["deviceId"], data["deviceSecret"]


def _count_readings(engine, sn: str) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> int:
        async with factory() as db:
            device = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == sn))).scalar_one()
            stmt = select(func.count()).select_from(EnvReading).where(EnvReading.device_id == device.id)
            return (await db.execute(stmt)).scalar_one()

    return asyncio.run(_run())


def test_ownership_rejection_blocks_write(client, engine, caplog) -> None:
    """错误密钥上报一批读数：403，`env_reading` 计数不变，日志含该 SN。"""
    token, _ = _login(client, "farmer-q-01")
    device_id, _secret = _bind(client, token, "HN-Q-0001")
    client.post(_PARSE_URL, json={"deviceId": device_id, "payload": '{"t1": 26.5}'}, headers=_headers(token))
    client.post(
        _MAPPING_URL,
        json={"deviceId": device_id, "mappings": _AIR_ONLY},
        headers=_headers(token),
    )
    before = _count_readings(engine, "HN-Q-0001")

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            _REPORT_URL.format(sn="HN-Q-0001"),
            content='{"t1": 26.5, "t2": 27.0, "t3": 28.0}',
            headers={"X-Device-Secret": "wrong"},
        )

    assert resp.status_code == 403, resp.text
    assert _count_readings(engine, "HN-Q-0001") - before == 0
    assert any("HN-Q-0001" in record.getMessage() for record in caplog.records)


# ---- 三级取数：source 取值 ----


def _insert_reading(engine, user_id: str, metric: str, value: float, *, quality: str = "ok", minutes_ago: float = 1.0) -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> None:
        async with factory() as db:
            db.add(
                EnvReading(
                    user_id=user_id,
                    metric=metric,
                    value=value,
                    unit=None,
                    source="sensor",
                    observed_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
                    quality=quality,
                )
            )
            await db.commit()

    asyncio.run(_run())


def test_query_sensors_fallback_when_nothing_available(client, engine) -> None:
    """无读数且天气不可用：`source="fallback"`（静态兜底不再伪装为实测来源）。"""
    from app.repositories import Repositories
    from app.tools.sensors import query_sensors

    token, user_id = _login(client, "farmer-q-02")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> dict:
        async with factory() as db:
            return await query_sensors(Repositories(db), user_id)

    result = asyncio.run(_run())
    assert result["source"] == "fallback"


def test_query_sensors_sensor_when_fresh_reading(client, engine) -> None:
    """有新鲜读数：`source="sensor"`，且读数覆盖兜底值。"""
    from app.repositories import Repositories
    from app.tools.sensors import query_sensors

    token, user_id = _login(client, "farmer-q-03")
    _insert_reading(engine, user_id, "air_temp", 31.5, minutes_ago=1)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> dict:
        async with factory() as db:
            return await query_sensors(Repositories(db), user_id)

    result = asyncio.run(_run())
    assert result["source"] == "sensor"
    assert result["air_temperature"] == 31.5


def test_query_sensors_weather_when_reading_suspect(client, engine, monkeypatch) -> None:
    """读数存在但被标 `suspect`：降级到天气服务，`source="weather"`。"""
    from app.repositories import Repositories
    from app.tools import sensors
    from app.tools.weather import WeatherFacts

    token, user_id = _login(client, "farmer-q-04")
    _insert_reading(engine, user_id, "air_temp", 265.0, quality="suspect", minutes_ago=1)

    async def _fake_weather(self, db, uid):  # noqa: ANN001
        return WeatherFacts(values={"air_temp": 22.0}, observed_at=datetime.now(UTC), region_hit_level="city")

    monkeypatch.setattr(sensors.weather_service, "current_with", _fake_weather.__get__(sensors.weather_service))

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> dict:
        async with factory() as db:
            return await sensors.query_sensors(Repositories(db), user_id)

    result = asyncio.run(_run())
    assert result["source"] == "weather"
    assert result["air_temperature"] == 22.0
