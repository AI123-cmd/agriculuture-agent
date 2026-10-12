"""T5-06 验收：设备上报接口与密钥校验。

覆盖文档"验收标准"全部条目：鉴权（正确/错误密钥、不存在 SN、已解绑设备）、三种不返回 5xx 的
失败路径（`payload_unparsable` / `no_mapping` / `mapping_missing`）、超范围读数标 `suspect` 且
保留原值、落库失败返回 `deferred`、上报成功后刷新 `last_report_at` 与 `online_status`、
未带密钥返回 422 且不触发业务查询。
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import DeviceConfig, EnvReading

_REPORT_URL = "/api/device/report/{sn}"
_BIND_URL = "/api/device/bind"
_PARSE_URL = "/api/device/parse-sample"
_MAPPING_URL = "/api/device/mapping"


def _login(client, code: str) -> tuple[str, str]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind(client, token: str, sn: str) -> tuple[str, str]:
    """绑定设备并返回 `(deviceId, deviceSecret)`。"""
    resp = client.post(_BIND_URL, json={"deviceSn": sn}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["deviceId"], data["deviceSecret"]


def _configure(client, token: str, device_id: str, sample: str, mappings: list[dict]) -> None:
    """走完三步接入的前两步半：样例解析（写缓存）+ 保存映射。"""
    resp = client.post(_PARSE_URL, json={"deviceId": device_id, "payload": sample}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    resp = client.post(_MAPPING_URL, json={"deviceId": device_id, "mappings": mappings}, headers=_headers(token))
    assert resp.status_code == 200, resp.text


def _report(client, sn: str, secret: str, body: str):
    return client.post(_REPORT_URL.format(sn=sn), content=body, headers={"X-Device-Secret": secret})


def _reading_rows(engine, sn: str) -> list[EnvReading]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> list[EnvReading]:
        async with factory() as db:
            device = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == sn))).scalar_one()
            stmt = select(EnvReading).where(EnvReading.device_id == device.id).order_by(EnvReading.metric)
            return list((await db.execute(stmt)).scalars().all())

    return asyncio.run(_run())


def _device_row(engine, sn: str) -> DeviceConfig:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> DeviceConfig:
        async with factory() as db:
            return (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == sn))).scalar_one()

    return asyncio.run(_run())


_AIR_ONLY = [{"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"}]


def test_report_accepted_equals_mapping_count(client, engine) -> None:
    token, _ = _login(client, "farmer-rep-01")
    device_id, secret = _bind(client, token, "HN-REP-0001")
    _configure(
        client,
        token,
        device_id,
        '{"t1": 26.5, "sm": 45}',
        [
            {"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"},
            {"rawField": "sm", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"},
        ],
    )

    resp = _report(client, "HN-REP-0001", secret, '{"t1": 26.8, "sm": 44}')

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted"] == 2
    assert data["flagged"] == 0
    assert data["note"] is None
    rows = _reading_rows(engine, "HN-REP-0001")
    assert {r.metric for r in rows} == {"air_temp", "soil_moisture"}
    assert all(r.quality == "ok" for r in rows)
    assert all(r.source == "sensor" for r in rows)


def test_report_wrong_secret_returns_403(client, engine) -> None:
    token, _ = _login(client, "farmer-rep-02")
    device_id, _secret = _bind(client, token, "HN-REP-0002")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    resp = _report(client, "HN-REP-0002", "wrong-secret", '{"t1": 26.5}')

    assert resp.status_code == 403, resp.text
    assert _reading_rows(engine, "HN-REP-0002") == []


def test_report_unknown_sn_returns_403(client, engine) -> None:
    resp = _report(client, "HN-NOT-EXIST", "whatever", '{"t1": 26.5}')
    assert resp.status_code == 403


def test_report_unbound_device_returns_403(client, engine) -> None:
    """已解绑设备的 SN：`get_by_sn` 过滤 `unbound_at`，返回 403。"""
    token, _ = _login(client, "farmer-rep-03")
    device_id, secret = _bind(client, token, "HN-REP-0003")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _unbind() -> None:
        async with factory() as db:
            row = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == "HN-REP-0003"))).scalar_one()
            from datetime import UTC, datetime

            row.unbound_at = datetime.now(UTC)
            await db.commit()

    asyncio.run(_unbind())

    resp = _report(client, "HN-REP-0003", secret, '{"t1": 26.5}')
    assert resp.status_code == 403, resp.text


# ---- 不返回 5xx 的失败路径 ----


def test_report_unparsable_payload_returns_200(client, engine) -> None:
    token, _ = _login(client, "farmer-rep-04")
    device_id, secret = _bind(client, token, "HN-REP-0004")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    resp = _report(client, "HN-REP-0004", secret, "{not json")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted"] == 0
    assert data["note"] == "payload_unparsable"


def test_report_no_mapping_returns_200(client, engine) -> None:
    """报文里没有映射字段（只有多余字段）：200 + `no_mapping`。"""
    token, _ = _login(client, "farmer-rep-05")
    device_id, secret = _bind(client, token, "HN-REP-0005")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    resp = _report(client, "HN-REP-0005", secret, '{"other": 1}')

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted"] == 0
    assert data["note"] == "no_mapping"


def test_report_mapping_missing_returns_200(client, engine) -> None:
    """设备已绑定但未配置映射：200 + `mapping_missing`。"""
    token, _ = _login(client, "farmer-rep-06")
    _device_id, secret = _bind(client, token, "HN-REP-0006")

    resp = _report(client, "HN-REP-0006", secret, '{"t1": 26.5}')

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted"] == 0
    assert data["note"] == "mapping_missing"


# ---- 异常数据标记而非丢弃 ----


def test_report_out_of_range_flagged_and_kept(client, engine) -> None:
    """上报值超量程：标 `suspect` 且保留原始换算值（标记而非丢弃）。"""
    token, _ = _login(client, "farmer-rep-07")
    device_id, secret = _bind(client, token, "HN-REP-0007")
    # 映射用 `scale=1` 且样例值本身在量程内（26.5），保存能通过换算级校验；
    # 上报时发 265，同一映射换算得 265°C 超空气温度上限，触发 `suspect`。
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    resp = _report(client, "HN-REP-0007", secret, '{"t1": 265}')

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["flagged"] >= 1
    rows = _reading_rows(engine, "HN-REP-0007")
    air = next(r for r in rows if r.metric == "air_temp")
    assert air.quality == "suspect"
    assert float(air.value) == 265.0


def test_report_stale_timestamp_flagged(client, engine) -> None:
    """时间戳 25 小时前：标 `stale`，仍落库。"""
    token, _ = _login(client, "farmer-rep-08")
    device_id, secret = _bind(client, token, "HN-REP-0008")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    old_ts = 1_700_000_000  # 2023-11-14，远超 24 小时
    resp = _report(client, "HN-REP-0008", secret, f'{{"t1": 26.5, "ts": {old_ts}}}')

    assert resp.status_code == 200, resp.text
    assert resp.json()["flagged"] == 1
    rows = _reading_rows(engine, "HN-REP-0008")
    assert rows[0].quality == "stale"


# ---- 落库失败 → deferred ----


def test_report_db_failure_returns_deferred(client, engine, monkeypatch) -> None:
    token, _ = _login(client, "farmer-rep-09")
    device_id, secret = _bind(client, token, "HN-REP-0009")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    from app.services import env_service

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(env_service.env_reading_repo, "bulk_insert", _boom)

    resp = _report(client, "HN-REP-0009", secret, '{"t1": 26.8}')

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted"] == 0
    assert data["note"] == "deferred"


# ---- 上报成功刷新设备状态 ----


def test_report_updates_device_status(client, engine) -> None:
    token, _ = _login(client, "farmer-rep-10")
    device_id, secret = _bind(client, token, "HN-REP-0010")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    assert _report(client, "HN-REP-0010", secret, '{"t1": 26.8}').status_code == 200

    row = _device_row(engine, "HN-REP-0010")
    assert row.last_report_at is not None
    assert row.online_status == "online"


# ---- 未鉴权：422 且不触发业务查询 ----


def test_report_missing_secret_header_returns_422(client, engine, monkeypatch) -> None:
    """缺失 `X-Device-Secret` 由 FastAPI 在进入业务逻辑前拦下：422 且不触发任何业务查询。"""
    token, _ = _login(client, "farmer-rep-11")
    device_id, _secret = _bind(client, token, "HN-REP-0011")
    _configure(client, token, device_id, '{"t1": 26.5}', _AIR_ONLY)

    from app.services import env_service

    calls: list[str] = []
    original = env_service.device_config_repo.get_by_sn

    async def _spy(db, device_sn):  # noqa: ANN001
        calls.append(device_sn)
        return await original(db, device_sn)

    monkeypatch.setattr(env_service.device_config_repo, "get_by_sn", _spy)

    resp = client.post(_REPORT_URL.format(sn="HN-REP-0011"), content='{"t1": 26.5}')

    assert resp.status_code == 422, resp.text
    # 请求头缺失在依赖解析阶段即失败，业务查询（按 SN 定位设备）不应被触发。
    assert calls == []
