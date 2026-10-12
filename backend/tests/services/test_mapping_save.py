"""T5-05 验收：字段映射保存与三层校验。

用例覆盖文档"验收标准"的全部条目：字段级（枚举外指标、字段不在样例报文内）、映射级（重复
`rawField` / 重复 `standardMetric`）、换算级（样例值试算超区间）、`unit` 静默覆盖、P0 缺失只
告警、整体替换与失败回滚、越权 404、预览与 `parse_payload` 逐字段相等。

样例报文的字段来源校验依赖 T5-04 的短时缓存，故每个用例先打一次 `/api/device/parse-sample`
把样例写入缓存，再提交映射——这与农户的真实操作顺序一致。
"""

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import FieldMapping

_MAPPING_URL = "/api/device/mapping"
_PARSE_URL = "/api/device/parse-sample"
_BIND_URL = "/api/device/bind"


def _login(client, code: str) -> tuple[str, str]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind_and_get_device(client, token: str, sn: str) -> str:
    resp = client.post(_BIND_URL, json={"deviceSn": sn}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["deviceId"]


def _seed_sample(client, token: str, device_id: str, payload: str) -> None:
    """让设备"最近一次样例报文"落到短时缓存（第三步校验字段来源时要用）。"""
    resp = client.post(_PARSE_URL, json={"deviceId": device_id, "payload": payload}, headers=_headers(token))
    assert resp.status_code == 200, resp.text


def _save(client, token: str, device_id: str, mappings: list[dict]):
    return client.post(_MAPPING_URL, json={"deviceId": device_id, "mappings": mappings}, headers=_headers(token))


def _mapping_rows(engine, device_id: str) -> list[FieldMapping]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> list[FieldMapping]:
        async with factory() as db:
            stmt = select(FieldMapping).where(FieldMapping.device_id == device_id).order_by(FieldMapping.raw_field)
            return list((await db.execute(stmt)).scalars().all())

    return asyncio.run(_run())


def _count_mappings(engine, device_id: str) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> int:
        async with factory() as db:
            stmt = select(func.count()).select_from(FieldMapping).where(FieldMapping.device_id == device_id)
            return (await db.execute(stmt)).scalar_one()

    return asyncio.run(_run())


# ---- 字段级 ----


def test_save_legal_mappings_success(client, engine) -> None:
    token, _ = _login(client, "farmer-map-01")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0001")
    _seed_sample(client, token, device_id, '{"air_temp": 26.5, "soil_moisture": 45}')

    resp = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "air_temp", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"},
            {"rawField": "soil_moisture", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"},
        ],
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["saved"] == 2
    assert data["skipped"] == []
    assert _count_mappings(engine, device_id) == 2


def test_save_rejects_metric_out_of_enum(client, engine) -> None:
    """`standardMetric="temp"` 不在枚举内：400，`layer=field`。"""
    token, _ = _login(client, "farmer-map-02")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0002")
    _seed_sample(client, token, device_id, '{"t1": 26.5}')

    resp = _save(client, token, device_id, [{"rawField": "t1", "standardMetric": "temp", "unit": "°C"}])

    assert resp.status_code == 400, resp.text
    assert resp.json()["data"]["layer"] == "field"
    assert _count_mappings(engine, device_id) == 0


def test_save_rejects_field_not_in_sample(client, engine) -> None:
    """`rawField` 不在最近一次样例报文内：400，`layer=field`。"""
    token, _ = _login(client, "farmer-map-03")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0003")
    _seed_sample(client, token, device_id, '{"air_temp": 26.5}')

    resp = _save(client, token, device_id, [{"rawField": "t9", "standardMetric": "air_temp", "unit": "°C"}])

    assert resp.status_code == 400, resp.text
    assert resp.json()["data"]["layer"] == "field"


# ---- 映射级 ----


def test_save_rejects_duplicate_raw_field(client, engine) -> None:
    token, _ = _login(client, "farmer-map-04")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0004")
    _seed_sample(client, token, device_id, '{"air_temp": 26.5}')

    resp = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "air_temp", "standardMetric": "air_temp", "unit": "°C"},
            {"rawField": "air_temp", "standardMetric": "soil_temp", "unit": "°C"},
        ],
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["data"]["layer"] == "mapping"


def test_save_rejects_duplicate_metric(client, engine) -> None:
    token, _ = _login(client, "farmer-map-05")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0005")
    _seed_sample(client, token, device_id, '{"t1": 26.5, "t2": 27.0}')

    resp = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "t1", "standardMetric": "air_temp", "unit": "°C"},
            {"rawField": "t2", "standardMetric": "air_temp", "unit": "°C"},
        ],
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["data"]["layer"] == "mapping"


def test_save_missing_p0_only_warns(client, engine) -> None:
    """只提交 `soil_moisture`：保存成功，`warnings` 提示缺失的 P0 指标。"""
    token, _ = _login(client, "farmer-map-06")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0006")
    _seed_sample(client, token, device_id, '{"soil_moisture": 45}')

    resp = _save(
        client,
        token,
        device_id,
        [{"rawField": "soil_moisture", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"}],
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["saved"] == 1
    assert data["warnings"]
    assert any("空气温度" in w for w in data["warnings"])


# ---- 换算级 ----


def test_save_rejects_scale_out_of_range(client, engine) -> None:
    """`t1=265` 映射为空气温度且 `scaleFactor=1`：试算 265°C 超区间，`layer=scale`。"""
    token, _ = _login(client, "farmer-map-07")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0007")
    _seed_sample(client, token, device_id, '{"t1": 265}')

    resp = _save(
        client,
        token,
        device_id,
        [{"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "265", "scaleFactor": 1.0}],
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["data"]["layer"] == "scale"
    assert _count_mappings(engine, device_id) == 0


def test_save_scale_factor_fix_succeeds(client, engine) -> None:
    """同一条把 `scaleFactor` 改为 0.1：试算 26.5°C 合法，保存成功。"""
    token, _ = _login(client, "farmer-map-08")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0008")
    _seed_sample(client, token, device_id, '{"t1": 265}')

    resp = _save(
        client,
        token,
        device_id,
        [{"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "265", "scaleFactor": 0.1}],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["saved"] == 1
    rows = _mapping_rows(engine, device_id)
    assert float(rows[0].scale_factor) == 0.1


def test_save_skips_scale_check_for_non_numeric_sample(client, engine) -> None:
    """样例值不可数值化（`ON`）：跳过试算而不是拒绝。"""
    token, _ = _login(client, "farmer-map-09")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0009")
    _seed_sample(client, token, device_id, '{"switch": "ON"}')

    resp = _save(
        client,
        token,
        device_id,
        [{"rawField": "switch", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "ON"}],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["saved"] == 1


# ---- 单位静默覆盖 ----


def test_save_overrides_unit_variants(client, engine) -> None:
    """`unit="℃"` 与 `unit="乱七八糟"` 均被覆盖为该指标默认单位 `°C`。"""
    token, _ = _login(client, "farmer-map-10")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0010")
    _seed_sample(client, token, device_id, '{"t1": 26.5, "t2": 27.0}')

    resp = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "t1", "standardMetric": "air_temp", "unit": "℃", "sampleRaw": "26.5"},
            {"rawField": "t2", "standardMetric": "soil_temp", "unit": "乱七八糟", "sampleRaw": "27.0"},
        ],
    )

    assert resp.status_code == 200, resp.text
    units = {row.raw_field: row.unit for row in _mapping_rows(engine, device_id)}
    assert units == {"t1": "°C", "t2": "°C"}


# ---- 整体替换与回滚 ----


def test_save_replaces_all_mappings(client, engine) -> None:
    """先存 3 条再提交 1 条：库中只剩 1 条（整体替换语义）。"""
    token, _ = _login(client, "farmer-map-11")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0011")
    _seed_sample(client, token, device_id, '{"t1": 26.5, "h1": 78, "sm": 45}')

    first = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"},
            {"rawField": "h1", "standardMetric": "air_humidity", "unit": "%", "sampleRaw": "78"},
            {"rawField": "sm", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"},
        ],
    )
    assert first.status_code == 200, first.text
    assert _count_mappings(engine, device_id) == 3

    second = _save(
        client,
        token,
        device_id,
        [{"rawField": "sm", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"}],
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["skipped"] == ["h1", "t1"]
    assert _count_mappings(engine, device_id) == 1


def test_save_failure_keeps_previous_mappings(client, engine) -> None:
    """替换过程中校验失败：事务不产生部分写入，旧映射保持 3 条。"""
    token, _ = _login(client, "farmer-map-12")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0012")
    _seed_sample(client, token, device_id, '{"t1": 26.5, "h1": 78, "sm": 45}')

    ok_resp = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"},
            {"rawField": "h1", "standardMetric": "air_humidity", "unit": "%", "sampleRaw": "78"},
            {"rawField": "sm", "standardMetric": "soil_moisture", "unit": "%", "sampleRaw": "45"},
        ],
    )
    assert ok_resp.status_code == 200, ok_resp.text

    bad = _save(
        client,
        token,
        device_id,
        [{"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "265", "scaleFactor": 1.0}],
    )
    assert bad.status_code == 400, bad.text
    assert _count_mappings(engine, device_id) == 3


# ---- 越权 ----


def test_save_other_user_device_returns_404(client, engine) -> None:
    """u2 对 u1 的设备保存映射返回 404，且 u1 的映射未被删除。"""
    token_a, _ = _login(client, "farmer-map-13a")
    token_b, _ = _login(client, "farmer-map-13b")
    device_id = _bind_and_get_device(client, token_a, "HN-MAP-0013")
    _seed_sample(client, token_a, device_id, '{"air_temp": 26.5}')
    assert (
        _save(
            client,
            token_a,
            device_id,
            [{"rawField": "air_temp", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"}],
        ).status_code
        == 200
    )

    resp = _save(
        client,
        token_b,
        device_id,
        [{"rawField": "air_temp", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "26.5"}],
    )

    assert resp.status_code == 404, resp.text
    assert _count_mappings(engine, device_id) == 1


def test_save_without_token_returns_401(client, engine) -> None:
    resp = client.post(_MAPPING_URL, json={"deviceId": "x", "mappings": []})
    assert resp.status_code == 401


def test_save_rejects_extra_field(client, engine) -> None:
    """`extra="forbid"`：请求体多带字段返回 422。"""
    token, _ = _login(client, "farmer-map-14")
    resp = client.post(_MAPPING_URL, json={"deviceId": "x", "mappings": [], "extra": 1}, headers=_headers(token))
    assert resp.status_code == 422


# ---- 预览与 parse_payload 逐字段相等 ----


def test_preview_matches_parse_payload(client, engine) -> None:
    """预览值由后端实跑 `parse_payload` 得出，逐字段等于直接调用该函数的结果。"""
    from app.services.env_service import parse_payload

    token, _ = _login(client, "farmer-map-15")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0015")
    sample = '{"t1": 265, "h1": 78}'
    _seed_sample(client, token, device_id, sample)

    resp = _save(
        client,
        token,
        device_id,
        [
            {"rawField": "t1", "standardMetric": "air_temp", "unit": "°C", "sampleRaw": "265", "scaleFactor": 0.1},
            {"rawField": "h1", "standardMetric": "air_humidity", "unit": "%", "sampleRaw": "78"},
        ],
    )
    assert resp.status_code == 200, resp.text
    preview = {row["rawField"]: row["value"] for row in resp.json()["data"]["preview"]}
    assert preview == {"t1": 26.5, "h1": 78.0}

    rows = _mapping_rows(engine, device_id)
    expected = {r.raw_field: r.value for r in parse_payload({"t1": 265, "h1": 78}, rows)}
    assert preview == expected


# ---- 样例缓存未命中时退化为弱校验 ----


def test_save_without_sample_cache_still_saves(client, engine) -> None:
    """未打过样例报文（缓存未命中）：跳过字段来源校验，合法映射仍可保存。"""
    token, _ = _login(client, "farmer-map-16")
    device_id = _bind_and_get_device(client, token, "HN-MAP-0016")

    resp = _save(
        client,
        token,
        device_id,
        [{"rawField": "whatever", "standardMetric": "air_temp", "unit": "°C"}],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["saved"] == 1
