"""天气数据集成（T3-02 / T5-08）：返回结构化当前天气，不可用一律返回 `None`。

与旧实现的差别：不再返回 `"多云 26°C"` 这类硬编码兜底文本。天气是增强项，其失败
不得让快照生成整体失败，也不得用推测值填充（产品说明书 6.4 红线四）。HTTP 超时、
配额耗尽、地区无覆盖三种情况都统一返回 `None`，调用方不需要区分。
"""

from datetime import UTC, datetime

import httpx

from app.core.config import get_settings


def _now() -> datetime:
    return datetime.now(UTC)


def current(region: str) -> dict | None:
    """按地区取当前天气；不可用返回 `None`。

    返回结构：`{"values": {标准指标: 数值}, "observed_at": datetime}`。
    `values` 的键用 `StandardMetric` 取值（`air_temp` / `air_humidity` / `rainfall`
    / `wind_speed`），与 `env_reading.metric` 对齐，使配置能逐指标登记来源。
    """
    settings = get_settings()
    if not settings.WEATHER_API_URL or not region:
        return None

    try:
        resp = httpx.get(
            settings.WEATHER_API_URL,
            params={"key": settings.WEATHER_API_KEY, "location": region},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001  超时/配额/网络异常统一视为不可用
        return None

    values: dict[str, float] = {}
    for metric, keys in (
        ("air_temp", ("air_temp", "temperature", "temp")),
        ("air_humidity", ("air_humidity", "humidity")),
        ("rainfall", ("rainfall", "precip", "rain")),
        ("wind_speed", ("wind_speed", "windSpeed", "wind")),
    ):
        for key in keys:
            raw = data.get(key)
            if raw is None:
                continue
            try:
                values[metric] = float(raw)
            except (TypeError, ValueError):
                continue
            break

    if not values:
        return None
    return {"values": values, "observed_at": _now()}


def get_weather_summary(location_name: str = "") -> str:
    """天气摘要文本（地点设置回显用）。不可用时返回空串，不构造推测文案。"""
    facts = current(location_name)
    if facts is None:
        return ""
    temp = facts["values"].get("air_temp")
    humidity = facts["values"].get("air_humidity")
    parts = []
    if temp is not None:
        parts.append(f"{temp:.0f}°C")
    if humidity is not None:
        parts.append(f"湿度{humidity:.0f}%")
    return " ".join(parts)


__all__ = ["current", "get_weather_summary"]
