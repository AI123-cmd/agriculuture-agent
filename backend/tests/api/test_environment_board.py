"""T5-08 验收：环境看板接口与来源标注。

覆盖文档"验收标准"全部条目：

1. 四条来源用例（`sensor` / `sensor_stale` / `weather` / `none`）各自的 `source`、`freshnessNote`
   与 `label`；
2. 同一农户在 `collect_env_facts` 与看板接口两处得到相同的 `source`；
3. 状态标注（阈值内 `normal` / 超上限 `high` / `stale` 与 `suspect` 均 `unknown` 但保留原值）；
4. 地区回退命中级标注（天气来源给 `city`，传感器来源为 `None`）；
5. 旧接口回归（`/summary` 仍返回原字段）与"不再走硬编码兜底"（服务源码不含 `26.5` / `58000`）；
6. `_freshness_note` 对 40 分钟前返回"数据为 1 小时前"。

来源用例直接向 `env_reading` 落行并 monkeypatch 天气服务，不经设备上报链路——本项验的是看板
对同一份环境事实的标注，而非上报（上报由 T5-06/T5-07 覆盖）。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.broadcast.inputs.env_facts import EnvFacts, collect_env_facts
from app.domain.metrics import StandardMetric
from app.models import EnvReading
from app.schemas.environment import SourceBadge
from app.services.environment_service import _badge_of, _freshness_note, _metric_quality, _metric_status, _to_board_metric
from app.tools import weather as weather_mod
from app.tools.weather import WeatherFacts

_BOARD_URL = "/api/environment/board"
_SUMMARY_URL = "/api/environment/summary"
_DETAIL_URL = "/api/environment/detail"


def _login(client, code: str) -> tuple[str, str]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _insert_reading(
    engine, user_id: str, metric: str, value: float, *, quality: str = "ok", minutes_ago: float = 1.0
) -> None:
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


def _patch_weather(monkeypatch, facts: WeatherFacts | None) -> None:
    async def _current(user_id: str) -> WeatherFacts | None:
        return facts

    monkeypatch.setattr(weather_mod.weather_service, "current", _current)


def _board(client, token: str) -> dict:
    resp = client.get(_BOARD_URL, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _metric_of(board: dict, metric: str) -> dict:
    return next(m for m in board["metrics"] if m["metric"] == metric)


# ---- 四条来源用例 ----


def test_board_sensor_fresh(client, engine, monkeypatch) -> None:
    """30 分钟内的 ok 读数：source=sensor 且无时效标注。"""
    token, user_id = _login(client, "farmer-board-01")
    _insert_reading(engine, user_id, "air_temp", 26.5, minutes_ago=5)
    _patch_weather(monkeypatch, None)

    board = _board(client, token)

    assert board["sourceBadge"]["source"] == "sensor"
    assert board["sourceBadge"]["freshnessNote"] is None
    assert board["sourceBadge"]["label"] == "数据来源：本地传感器"


def test_board_sensor_stale(client, engine, monkeypatch) -> None:
    """3 小时前的读数：source=sensor_stale 且时效标注为"数据为 3 小时前"。"""
    token, user_id = _login(client, "farmer-board-02")
    _insert_reading(engine, user_id, "soil_moisture", 23.0, minutes_ago=3 * 60)
    _patch_weather(monkeypatch, None)

    board = _board(client, token)

    assert board["sourceBadge"]["source"] == "sensor_stale"
    assert board["sourceBadge"]["freshnessNote"] == "数据为 3 小时前"


def test_board_weather(client, engine, monkeypatch) -> None:
    """无传感器但天气可用：source=weather 且无时效标注。"""
    token, _ = _login(client, "farmer-board-03")
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 30.0}, datetime.now(UTC), "city"))

    board = _board(client, token)

    assert board["sourceBadge"]["source"] == "weather"
    assert board["sourceBadge"]["freshnessNote"] is None
    assert board["sourceBadge"]["label"] == "数据来源：公共天气"


def test_board_none(client, engine, monkeypatch) -> None:
    """两者都不可用：source=none 且 label 含"暂无环境数据"。"""
    token, _ = _login(client, "farmer-board-04")
    _patch_weather(monkeypatch, None)

    board = _board(client, token)

    assert board["sourceBadge"]["source"] == "none"
    assert "暂无环境数据" in board["sourceBadge"]["label"]
    assert all(m["value"] is None for m in board["metrics"])


# ---- 与 collect_env_facts 同源 ----


def test_board_source_matches_collect_env_facts(client, engine, monkeypatch) -> None:
    """看板与 `collect_env_facts` 对同一份数据给出一致的 `source`（不得两处分叉）。"""
    token, user_id = _login(client, "farmer-board-05")
    _insert_reading(engine, user_id, "air_temp", 26.5, minutes_ago=120)
    _patch_weather(monkeypatch, None)

    board = _board(client, token)
    facts = asyncio.run(collect_env_facts(user_id, None))

    assert board["sourceBadge"]["source"] == facts.source


# ---- 状态标注 ----


def test_board_status_normal_within_threshold(client, engine, monkeypatch) -> None:
    token, user_id = _login(client, "farmer-board-06")
    _insert_reading(engine, user_id, "air_temp", 26.5, minutes_ago=5)
    _patch_weather(monkeypatch, None)

    assert _metric_of(_board(client, token), "air_temp")["status"] == "normal"


def test_board_status_high_over_threshold(client, engine, monkeypatch) -> None:
    """42°C 超 `BROADCAST_TEMP_HIGH`：status=high。"""
    token, user_id = _login(client, "farmer-board-07")
    _insert_reading(engine, user_id, "air_temp", 42.0, minutes_ago=5)
    _patch_weather(monkeypatch, None)

    assert _metric_of(_board(client, token), "air_temp")["status"] == "high"


def test_board_status_unknown_for_stale_but_value_kept(client, engine, monkeypatch) -> None:
    """stale 读数：status=unknown，但 value 仍为原始数值。"""
    token, user_id = _login(client, "farmer-board-08")
    _insert_reading(engine, user_id, "air_temp", 26.5, minutes_ago=3 * 60)
    _patch_weather(monkeypatch, None)

    metric = _metric_of(_board(client, token), "air_temp")

    assert metric["status"] == "unknown"
    assert metric["value"] == 26.5
    assert metric["quality"] == "stale"


def test_status_unknown_for_suspect_reading() -> None:
    """suspect 读数：status=unknown（坏数据不算"偏高"），value 仍为原值。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 265.0}, observed_at=datetime.now(UTC))

    metric = _to_board_metric(StandardMetric.AIR_TEMP, facts)

    assert metric.status == "unknown"
    assert metric.quality == "suspect"
    assert metric.value == 265.0


# ---- 地区回退命中级 ----


def test_region_hit_level_city_for_weather(client, engine, monkeypatch) -> None:
    token, _ = _login(client, "farmer-board-09")
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 30.0}, datetime.now(UTC), "city"))

    assert _board(client, token)["sourceBadge"]["regionHitLevel"] == "city"


def test_region_hit_level_none_for_sensor(client, engine, monkeypatch) -> None:
    token, user_id = _login(client, "farmer-board-10")
    _insert_reading(engine, user_id, "air_temp", 26.5, minutes_ago=5)
    _patch_weather(monkeypatch, WeatherFacts({"air_temp": 30.0}, datetime.now(UTC), "city"))

    assert _board(client, token)["sourceBadge"]["regionHitLevel"] is None


# ---- 旧接口回归 ----


def test_summary_keeps_legacy_fields(client, engine, monkeypatch) -> None:
    token, user_id = _login(client, "farmer-board-11")
    _insert_reading(engine, user_id, "air_temp", 26.5, minutes_ago=5)
    _patch_weather(monkeypatch, None)

    resp = client.get(_SUMMARY_URL, headers=_headers(token))

    assert resp.status_code == 200
    data = resp.json()["data"]
    for field in ("temperature", "humidity", "soilStatus", "soilDetail", "lightStatus", "lightDetail", "lastUpdateAt"):
        assert field in data, field
    assert "sourceBadge" in data


def test_detail_has_source_badge_and_only_own_devices(client, engine, monkeypatch) -> None:
    """`/detail` 结构保持原样并新增 `sourceBadge`；设备段仍只含本人设备。"""
    token, _ = _login(client, "farmer-board-12")
    _patch_weather(monkeypatch, None)

    resp = client.get(_DETAIL_URL, headers=_headers(token))

    assert resp.status_code == 200
    data = resp.json()["data"]
    for field in ("metrics", "airData", "soilData", "weatherData", "devices", "lastUpdateAt", "sourceBadge"):
        assert field in data, field
    assert data["devices"] == []


def test_service_has_no_hardcoded_fallback_values() -> None:
    """服务源码不含旧硬编码兜底值（`26.5` / `58000`），兜底不再伪装为实测。"""
    import app.services.environment_service as module

    source = __import__("pathlib").Path(module.__file__).read_text(encoding="utf-8")
    assert "26.5" not in source
    assert "58000" not in source


# ---- 时效标注单元用例 ----


def test_freshness_note_40_minutes_is_one_hour() -> None:
    """40 分钟前 → "数据为 1 小时前"，不返回"0 小时前"。"""
    observed_at = datetime.now(UTC) - timedelta(minutes=40)
    assert _freshness_note(observed_at, "sensor_stale") == "数据为 1 小时前"


def test_freshness_note_only_for_stale_source() -> None:
    """`sensor` / `weather` / `none` 一律无时效标注。"""
    observed_at = datetime.now(UTC) - timedelta(hours=3)
    assert _freshness_note(observed_at, "sensor") is None
    assert _freshness_note(observed_at, "weather") is None
    assert _freshness_note(observed_at, "none") is None


def test_freshness_note_none_observed_at() -> None:
    assert _freshness_note(None, "sensor_stale") is None


def test_badge_label_covers_all_four_sources() -> None:
    """四值文案全覆盖，不留空。"""
    for source in ("sensor", "sensor_stale", "weather", "none"):
        badge = _badge_of(EnvFacts(source=source, values={}))
        assert isinstance(badge, SourceBadge)
        assert badge.label


def test_metric_quality_marks_out_of_range_suspect() -> None:
    assert _metric_quality("air_temp", 265.0, "sensor") == "suspect"
    assert _metric_quality("air_temp", 26.5, "sensor") == "ok"
    assert _metric_quality("air_temp", 26.5, "sensor_stale") == "stale"
    assert _metric_quality("air_temp", None, "none") == "stale"


def test_metric_status_unknown_when_not_ok() -> None:
    assert _metric_status("air_temp", 26.5, "suspect") == "unknown"
    assert _metric_status("air_temp", 26.5, "stale") == "unknown"
    assert _metric_status("air_temp", None, "ok") == "unknown"


def test_metric_status_low_below_threshold() -> None:
    """土壤含水率低于干旱阈值：status=low。"""
    assert _metric_status("soil_moisture", 10.0, "ok") == "low"
