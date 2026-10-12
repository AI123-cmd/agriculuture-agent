"""T5-09 验收：设备列表与解绑。

覆盖文档"验收标准"全部条目：

1. 列表：本人**在绑**设备，不含他人；`onlineState` 四档（10 分钟前 `online`、1 小时前
   `offline` 且 `silenceNote` 含"小时"、10 天前 `fault`、从未上报 `offline` 且 `lastReportAt is None`）。
2. 解绑：`DELETE /api/device/{id}` 返回 200；解绑后不再出现在列表；`unbound_at` 非空；重复解绑幂等。
3. 失效：解绑后用原密钥与 SN 上报返回 403，且 `env_reading` 计数不变。
4. 重新绑定：解绑后再绑返回同一 `deviceId`，映射条数不变（未清除）。
5. 越权：他人令牌删自己的设备返回 404，且本设备的 `unbound_at` 仍为 `NULL`。
6. `mappedFields`：完成三步接入 >0，仅绑定未保存映射 =0。
7. `environment_service.detail()` 的 `deviceSn` 集合与 `GET /api/device/list` 一致。

在线状态靠最近上报时间反推，因此用例直接改写 `device_config.last_report_at` 造出各档时长，
不经设备上报链路（上报由 T5-06/T5-07 覆盖）。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import DeviceConfig, EnvReading, FieldMapping

_LIST_URL = "/api/device/list"
_BIND_URL = "/api/device/bind"
_PARSE_URL = "/api/device/parse-sample"
_MAPPING_URL = "/api/device/mapping"
_REPORT_URL = "/api/device/report/{sn}"
_DETAIL_URL = "/api/environment/detail"

_SAMPLE = '{"t1": 26.5, "sm": 45}'
_TWO_MAPPINGS = [
    {"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"},
    {"rawField": "sm", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"},
]


def _login(client, code: str) -> str:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind(client, token: str, sn: str) -> tuple[str, str]:
    """绑定并返回 `(deviceId, deviceSecret)`。"""
    resp = client.post(_BIND_URL, json={"deviceSn": sn}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["deviceId"], data["deviceSecret"]


def _configure(client, token: str, device_id: str) -> None:
    """走完三步接入：样例解析（写缓存）+ 保存映射。"""
    resp = client.post(_PARSE_URL, json={"deviceId": device_id, "payload": _SAMPLE}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    resp = client.post(_MAPPING_URL, json={"deviceId": device_id, "mappings": _TWO_MAPPINGS}, headers=_headers(token))
    assert resp.status_code == 200, resp.text


def _set_last_report(engine, sn: str, moment: datetime) -> None:
    """直接改写最近上报时刻，造出各档在线状态。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> None:
        async with factory() as db:
            row = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == sn))).scalar_one()
            row.last_report_at = moment
            await db.commit()

    asyncio.run(_run())


def _device_row(engine, sn: str) -> DeviceConfig:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> DeviceConfig:
        async with factory() as db:
            return (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == sn))).scalar_one()

    return asyncio.run(_run())


def _count(engine, model, **conditions) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> int:
        async with factory() as db:
            stmt = select(func.count()).select_from(model)
            for field, value in conditions.items():
                stmt = stmt.where(getattr(model, field) == value)
            return (await db.execute(stmt)).scalar_one()

    return asyncio.run(_run())


def _list_devices(client, token: str) -> list[dict]:
    resp = client.get(_LIST_URL, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["devices"]


def _by_sn(devices: list[dict], sn: str) -> dict:
    return next(d for d in devices if d["deviceSn"] == sn)


# ---- 列表：归属与在绑范围 ----


def test_list_returns_own_bound_devices_only(client, engine) -> None:
    """列表只含本人在绑设备；他人设备与已解绑设备都不出现。"""
    token_a = _login(client, "farmer-mng-01a")
    token_b = _login(client, "farmer-mng-01b")
    dev_a, _ = _bind(client, token_a, "HN-MNG-A001")
    _bind(client, token_b, "HN-MNG-B001")

    sns = [d["deviceSn"] for d in _list_devices(client, token_a)]

    assert sns == ["HN-MNG-A001"]
    assert dev_a in [d["id"] for d in _list_devices(client, token_a)]


def test_list_excludes_unbound_device(client, engine) -> None:
    token = _login(client, "farmer-mng-02")
    device_id, _ = _bind(client, token, "HN-MNG-0002")

    assert client.delete(f"/api/device/{device_id}", headers=_headers(token)).status_code == 200

    assert _list_devices(client, token) == []


# ---- 在线状态四档 ----


def test_online_state_online_within_threshold(client, engine) -> None:
    """10 分钟前上报：`online`，无静默提示。"""
    token = _login(client, "farmer-mng-03")
    _bind(client, token, "HN-MNG-0003")
    _set_last_report(engine, "HN-MNG-0003", datetime.now(UTC) - timedelta(minutes=10))

    item = _by_sn(_list_devices(client, token), "HN-MNG-0003")

    assert item["onlineState"] == "online"
    assert item["silenceNote"] is None
    assert item["lastReportAt"] is not None


def test_online_state_offline_with_hour_note(client, engine) -> None:
    """1 小时前上报：`offline`，并给出"小时"级静默提示。"""
    token = _login(client, "farmer-mng-04")
    _bind(client, token, "HN-MNG-0004")
    _set_last_report(engine, "HN-MNG-0004", datetime.now(UTC) - timedelta(hours=1))

    item = _by_sn(_list_devices(client, token), "HN-MNG-0004")

    assert item["onlineState"] == "offline"
    assert item["silenceNote"] is not None
    assert "小时" in item["silenceNote"]


def test_online_state_fault_after_long_silence(client, engine) -> None:
    """10 天前上报：`fault`（长期失联），提示长期失联文案。"""
    token = _login(client, "farmer-mng-05")
    _bind(client, token, "HN-MNG-0005")
    _set_last_report(engine, "HN-MNG-0005", datetime.now(UTC) - timedelta(days=10))

    item = _by_sn(_list_devices(client, token), "HN-MNG-0005")

    assert item["onlineState"] == "fault"
    assert item["silenceNote"] is not None
    assert "长期" in item["silenceNote"]


def test_online_state_offline_when_never_reported(client, engine) -> None:
    """从未上报：`offline` 且 `lastReportAt is None`，无静默提示（还没开始）。"""
    token = _login(client, "farmer-mng-06")
    _bind(client, token, "HN-MNG-0006")

    item = _by_sn(_list_devices(client, token), "HN-MNG-0006")

    assert item["onlineState"] == "offline"
    assert item["lastReportAt"] is None
    assert item["silenceNote"] is None


# ---- mappedFields：接入完成度 ----


def test_mapped_fields_zero_before_configured(client, engine) -> None:
    """仅绑定、未保存映射：`mappedFields == 0`（接入没走完）。"""
    token = _login(client, "farmer-mng-07")
    _bind(client, token, "HN-MNG-0007")

    assert _by_sn(_list_devices(client, token), "HN-MNG-0007")["mappedFields"] == 0


def test_mapped_fields_positive_after_configured(client, engine) -> None:
    token = _login(client, "farmer-mng-08")
    device_id, _ = _bind(client, token, "HN-MNG-0008")
    _configure(client, token, device_id)

    assert _by_sn(_list_devices(client, token), "HN-MNG-0008")["mappedFields"] == len(_TWO_MAPPINGS)


# ---- 解绑 ----


def test_unbind_sets_unbound_at_and_removes_from_list(client, engine) -> None:
    token = _login(client, "farmer-mng-09")
    device_id, _ = _bind(client, token, "HN-MNG-0009")

    resp = client.delete(f"/api/device/{device_id}", headers=_headers(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"unbound": True}
    assert _list_devices(client, token) == []
    assert _device_row(engine, "HN-MNG-0009").unbound_at is not None


def test_unbind_is_idempotent(client, engine) -> None:
    token = _login(client, "farmer-mng-10")
    device_id, _ = _bind(client, token, "HN-MNG-0010")

    first = client.delete(f"/api/device/{device_id}", headers=_headers(token))
    second = client.delete(f"/api/device/{device_id}", headers=_headers(token))

    assert first.status_code == 200
    assert second.status_code == 200, second.text


# ---- 解绑后旧设备即时失效 ----


def test_unbound_device_report_rejected_and_no_new_readings(client, engine) -> None:
    """解绑后原密钥上报 403，且 `env_reading` 计数不变（真正停止写入）。"""
    token = _login(client, "farmer-mng-11")
    device_id, secret = _bind(client, token, "HN-MNG-0011")
    _configure(client, token, device_id)
    before = _count(engine, EnvReading, device_id=device_id)

    client.delete(f"/api/device/{device_id}", headers=_headers(token))
    resp = client.post(_REPORT_URL.format(sn="HN-MNG-0011"), content=_SAMPLE, headers={"X-Device-Secret": secret})

    assert resp.status_code == 403, resp.text
    assert _count(engine, EnvReading, device_id=device_id) == before


# ---- 重新绑定：复用同一行、映射不丢 ----


def test_rebind_reuses_device_and_keeps_mappings(client, engine) -> None:
    token = _login(client, "farmer-mng-12")
    device_id, _ = _bind(client, token, "HN-MNG-0012")
    _configure(client, token, device_id)
    mappings_before = _count(engine, FieldMapping, device_id=device_id)

    client.delete(f"/api/device/{device_id}", headers=_headers(token))
    rebind_id, _secret = _bind(client, token, "HN-MNG-0012")

    assert rebind_id == device_id
    assert _device_row(engine, "HN-MNG-0012").unbound_at is None
    assert _count(engine, FieldMapping, device_id=device_id) == mappings_before


# ---- 越权：他人设备不可解绑 ----


def test_unbind_foreign_device_returns_404_and_leaves_it_bound(client, engine) -> None:
    """他人令牌解绑返回 404，且**不泄露存在性**：本设备的 `unbound_at` 仍为 `NULL`。"""
    token_owner = _login(client, "farmer-mng-13a")
    token_intruder = _login(client, "farmer-mng-13b")
    device_id, _ = _bind(client, token_owner, "HN-MNG-0013")

    resp = client.delete(f"/api/device/{device_id}", headers=_headers(token_intruder))

    assert resp.status_code == 404, resp.text
    assert _device_row(engine, "HN-MNG-0013").unbound_at is None
    assert _list_devices(client, token_owner) != []


# ---- 与环境详情收敛同一数据源 ----


def test_detail_devices_match_device_list_sns(client, engine) -> None:
    """`environment_service.detail()` 的 `deviceSn` 集合与 `GET /api/device/list` 一致。"""
    token = _login(client, "farmer-mng-14")
    dev_one, _ = _bind(client, token, "HN-MNG-0014")
    _configure(client, token, dev_one)
    _bind(client, token, "HN-MNG-0015")

    list_sns = {d["deviceSn"] for d in _list_devices(client, token)}
    resp = client.get(_DETAIL_URL, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    detail_sns = {d["deviceSn"] for d in resp.json()["data"]["devices"]}

    assert detail_sns == list_sns == {"HN-MNG-0014", "HN-MNG-0015"}


def test_detail_excludes_other_users_devices(client, engine) -> None:
    """环境详情的设备段只含本人设备。"""
    token_a = _login(client, "farmer-mng-16a")
    token_b = _login(client, "farmer-mng-16b")
    _bind(client, token_a, "HN-MNG-0016A")
    _bind(client, token_b, "HN-MNG-0016B")

    resp = client.get(_DETAIL_URL, headers=_headers(token_a))

    assert resp.status_code == 200, resp.text
    assert {d["deviceSn"] for d in resp.json()["data"]["devices"]} == {"HN-MNG-0016A"}
