"""T3-02 验收：环境事实三级回退采集。

用例聚焦"任何一级都不得用推测值填充"这条红线：传感器与天气服务被 monkeypatch 成
确定行为，逐条验证五个分支的 `source` / `values` / `staleness_note`，并断言完全
不可用时 `values` 为空字典而非含默认数字。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.broadcast.inputs.env_facts import EnvFacts, collect_env_facts, is_env_risk_blocked
from app.tools import sensors as sensors_mod
from app.tools import weather as weather_mod
from app.tools.sensors import Reading
from app.tools.weather import WeatherFacts

USER = "user-t302"


def _ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


def _patch_sensor(monkeypatch, reading: Reading | None) -> None:
    async def _latest(user_id: str, plot_id: str | None) -> Reading | None:
        return reading

    monkeypatch.setattr(sensors_mod.sensor_service, "latest", _latest)


def _patch_weather(monkeypatch, facts: WeatherFacts | None) -> None:
    async def _current(user_id: str) -> WeatherFacts | None:
        return facts

    monkeypatch.setattr(weather_mod.weather_service, "current", _current)


async def test_fresh_reading_yields_sensor(monkeypatch) -> None:
    """新鲜读数（30 分钟内、quality=ok）→ source=sensor，无时效标注。"""
    _patch_sensor(monkeypatch, Reading({"soil_temp": 25.0}, _ago(minutes=5), "ok"))
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 30.0}, _ago(minutes=1), "province"))

    facts = await collect_env_facts(USER, None)

    assert facts.source == "sensor"
    assert facts.staleness_note is None
    assert facts.values == {"soil_temp": 25.0}
    assert facts.provenance == {"soil_temp": "sensor"}


async def test_one_hour_old_reading_is_stale_with_note(monkeypatch) -> None:
    """陈旧读数（1 小时前）→ source=sensor_stale 且 staleness_note 含"小时前"。"""
    _patch_sensor(monkeypatch, Reading({"soil_moisture": 23.0}, _ago(hours=1, minutes=5), "ok"))
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 30.0}, _ago(minutes=1), "province"))

    facts = await collect_env_facts(USER, None)

    assert facts.source == "sensor_stale"
    assert facts.staleness_note is not None
    assert "小时前" in facts.staleness_note
    assert facts.values == {"soil_moisture": 23.0}


async def test_over_24_hours_falls_back_to_weather(monkeypatch) -> None:
    """超 24 小时回退到天气：source=weather，带命中地区级。"""
    _patch_sensor(monkeypatch, Reading({"soil_temp": 25.0}, _ago(hours=30), "ok"))
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 31.0}, _ago(minutes=2), "city"))

    facts = await collect_env_facts(USER, None)

    assert facts.source == "weather"
    assert facts.values == {"air_temp": 31.0}
    assert facts.region_hit_level == "city"
    assert facts.staleness_note is None


async def test_no_sensor_no_weather_yields_none(monkeypatch) -> None:
    """无传感器无天气 → source=none 且 values 为空字典（不填推测值）。"""
    _patch_sensor(monkeypatch, None)
    _patch_weather(monkeypatch, None)

    facts = await collect_env_facts(USER, None)

    assert facts.source == "none"
    assert facts.values == {}
    assert facts.observed_at is None


async def test_suspect_reading_is_skipped_to_weather(monkeypatch) -> None:
    """quality=suspect 的读数被跳过后仍能正确降级到天气。"""
    _patch_sensor(monkeypatch, Reading({"air_temp": 265.0}, _ago(minutes=1), "suspect"))
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 28.0}, _ago(minutes=1), "county"))

    facts = await collect_env_facts(USER, None)

    assert facts.source == "weather"
    assert facts.values == {"air_temp": 28.0}


async def test_suspect_reading_does_not_leak_without_weather(monkeypatch) -> None:
    """suspect 且天气也不可用时，可疑读数不得进入 values。"""
    _patch_sensor(monkeypatch, Reading({"air_temp": 265.0}, _ago(minutes=1), "suspect"))
    _patch_weather(monkeypatch, None)

    facts = await collect_env_facts(USER, None)

    assert facts.source == "none"
    assert facts.values == {}


def test_is_env_risk_blocked_only_for_env_risk() -> None:
    """source=none 时拦截环境风险类；其他来源类不受影响。"""
    none_facts = EnvFacts(source="none", values={})
    assert is_env_risk_blocked(none_facts, "env_risk") is True
    assert is_env_risk_blocked(none_facts, "followup") is False

    sensor_facts = EnvFacts(source="sensor", values={"soil_temp": 25.0})
    assert is_env_risk_blocked(sensor_facts, "env_risk") is False


@pytest.mark.parametrize("minutes_ago", [5, 29])
async def test_fresh_window_boundary(monkeypatch, minutes_ago: int) -> None:
    """30 分钟窗口内的读数判为 sensor（边界内含 29 分钟）。"""
    _patch_sensor(monkeypatch, Reading({"soil_temp": 25.0}, _ago(minutes=minutes_ago), "ok"))
    _patch_weather(monkeypatch, None)

    facts = await collect_env_facts(USER, None)

    assert facts.source == "sensor"
