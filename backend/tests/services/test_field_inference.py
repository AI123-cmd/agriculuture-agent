"""T5-04 验收：样例报文解析与两层字段推断。

`field_inference` 是纯函数，主体用例不连库（字段名启发式、数值范围启发式、候选生成、模板展开）。
接口层用例用真实 `TestClient` 打 `/api/device/parse-sample`，证明"容错解析 + 200 而非 5xx + 归属
校验"这些接口行为；模板路径需要设备行与模板行，走数据库。
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.metrics import StandardMetric
from app.services.field_inference import (
    IGNORE_OPTION,
    fields_from_template,
    flatten_payload,
    infer_fields,
    load_payload,
    match_by_range,
    template_covers,
)
from app.services.template_seed import seed_vendor_templates

pytestmark = pytest.mark.usefixtures("requires_postgres")

_PARSE_URL = "/api/device/parse-sample"


def _login(client, code: str) -> tuple[str, str]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind_and_get_device(client, engine, token: str, sn: str) -> str:
    resp = client.post("/api/device/bind", json={"deviceSn": sn}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["deviceId"]


def _seed_templates(engine) -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> None:
        async with factory() as db:
            await seed_vendor_templates(db)
            await db.commit()

    asyncio.run(_run())


# ---- 纯函数：容错解析 ----


def test_load_payload_plain_json() -> None:
    assert load_payload('{"air_temp": 26.5}') == {"air_temp": 26.5}


def test_load_payload_tolerates_fullwidth_quotes_and_trailing_comma() -> None:
    """全角引号与尾随逗号被清洗后可解析（农户从聊天窗口复制的常见形态）。"""
    assert load_payload("{“air_temp”: 26.5,}") == {"air_temp": 26.5}


def test_load_payload_rejects_non_object_and_garbage() -> None:
    assert load_payload("{不是json") is None
    assert load_payload("[]") is None
    assert load_payload("") is None


def test_flatten_payload_nested_and_array() -> None:
    """嵌套对象与数组展开成点分键，口径与模板 `field_spec` 一致。"""
    flat = flatten_payload({"data": {"air_temp": 26.5}, "dp": {"soil_temp": [{"v": 21.3}]}})
    assert flat == {"data.air_temp": 26.5, "dp.soil_temp.0.v": 21.3}


# ---- 纯函数：字段名启发式 ----


@pytest.mark.parametrize("raw", ["t1", "temp", "air_temp", "T1"])
def test_name_rule_air_temp(raw: str) -> None:
    field = infer_fields({raw: 26.3})[0]
    assert field.inferredMetric == StandardMetric.AIR_TEMP.value


@pytest.mark.parametrize("raw", ["h1", "hum", "air_humidity"])
def test_name_rule_air_humidity(raw: str) -> None:
    field = infer_fields({raw: 78})[0]
    assert field.inferredMetric == StandardMetric.AIR_HUMIDITY.value


def test_name_rule_soil_temp() -> None:
    assert infer_fields({"soil_temp": 21.3})[0].inferredMetric == StandardMetric.SOIL_TEMP.value


def test_nested_leaf_name_matches() -> None:
    """嵌套报文用最后一段做字段名匹配（`params.air_temp` → `air_temp`）。"""
    field = infer_fields({"params.air_temp": 26.5})[0]
    assert field.inferredMetric == StandardMetric.AIR_TEMP.value


# ---- 纯函数：数值范围启发式 ----


def test_range_temperature_candidates() -> None:
    """26.3（0~50 一位小数）候选含 `air_temp` 与 `soil_temp`。"""
    metrics, _ = match_by_range(26.3)
    assert StandardMetric.AIR_TEMP in metrics
    assert StandardMetric.SOIL_TEMP in metrics


def test_range_humidity_candidates() -> None:
    """78（0~100 整数）候选含 `air_humidity` 与 `soil_moisture`。"""
    metrics, _ = match_by_range(78)
    assert StandardMetric.AIR_HUMIDITY in metrics
    assert StandardMetric.SOIL_MOISTURE in metrics


def test_range_soil_ph() -> None:
    """6.6（0~14 两位小数）推断为土壤 pH。"""
    metrics, _ = match_by_range(6.6)
    assert metrics == [StandardMetric.SOIL_PH]


def test_range_boolish_not_inferred() -> None:
    """0/1 或 `ON`/`OFF` 视为开关量，不推断指标。"""
    assert match_by_range("ON")[0] == []
    assert match_by_range(True)[0] == []


# ---- 纯函数：合并与候选 ----


def test_merge_name_beats_range() -> None:
    """字段名与范围一致时置信度相加并截断；`t1=26.3` 置信度高于单层。"""
    field = infer_fields({"t1": 26.3})[0]
    assert field.inferredMetric == StandardMetric.AIR_TEMP.value
    assert field.confidence > 0.7


def test_candidates_head_is_inferred_and_tail_is_ignore() -> None:
    """`candidates[0] == inferredMetric`（非空时），末位为忽略项。"""
    field = infer_fields({"t1": 26.3})[0]
    assert field.candidates[0] == field.inferredMetric
    assert field.candidates[-1] == IGNORE_OPTION


def test_battery_not_inferred_with_ignore_tail() -> None:
    """`battery=3.7` 不推断指标，候选末位为忽略项。"""
    field = infer_fields({"battery": 3.7})[0]
    assert field.inferredMetric is None
    assert field.candidates[-1] == IGNORE_OPTION


def test_status_field_has_empty_metric_answer() -> None:
    """`rssi=-71` 这类负值/状态字段不推断。"""
    field = infer_fields({"rssi": -71})[0]
    assert field.inferredMetric is None


# ---- 纯函数：模板展开 ----


def test_template_covers_subset_rule() -> None:
    assert template_covers({"a": 1, "b": 2}, {"a": 1, "b": 2, "c": 3}) is True
    assert template_covers({"a": 1, "z": 2}, {"a": 1, "b": 2}) is False


def test_fields_from_template_confidence_one() -> None:
    """模板展开：`confidence == 1.0`，`inferredMetric` 与 `field_spec` 一致，`sampleValue` 取实际值。"""
    spec = {"air_temp": {"standard_metric": "air_temp", "sample_value": 265}}
    fields = fields_from_template(spec, {"air_temp": 270})
    assert fields[0].confidence == 1.0
    assert fields[0].inferredMetric == StandardMetric.AIR_TEMP.value
    assert fields[0].sampleValue == "270"


# ---- 接口层：失败路径均返回 200 ----


def test_parse_sample_bad_json_returns_200_not_parsed(client, engine) -> None:
    token, _ = _login(client, "farmer-parse-01")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0001")

    resp = client.post(_PARSE_URL, json={"deviceId": device_id, "payload": "{不是json"}, headers=_headers(token))

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["parsed"] is False
    assert data["inferError"]


def test_parse_sample_array_returns_not_parsed(client, engine) -> None:
    token, _ = _login(client, "farmer-parse-02")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0002")

    resp = client.post(_PARSE_URL, json={"deviceId": device_id, "payload": "[]"}, headers=_headers(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["parsed"] is False


def test_parse_sample_only_status_fields_returns_not_parsed(client, engine) -> None:
    token, _ = _login(client, "farmer-parse-03")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0003")

    resp = client.post(
        _PARSE_URL,
        json={"deviceId": device_id, "payload": '{"ip":"1.2.3.4","rssi":-71}'},
        headers=_headers(token),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["parsed"] is False
    assert data["inferError"]


def test_parse_sample_heuristic_success(client, engine) -> None:
    """无模板设备：启发式推断成功，字段被识别。"""
    token, _ = _login(client, "farmer-parse-04")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0004")

    resp = client.post(
        _PARSE_URL,
        json={"deviceId": device_id, "payload": '{"t1": 26.3, "hum": 78}'},
        headers=_headers(token),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["parsed"] is True
    metrics = {f["rawField"]: f["inferredMetric"] for f in data["fields"]}
    assert metrics == {"t1": "air_temp", "hum": "air_humidity"}


def test_parse_sample_dirty_payload_success(client, engine) -> None:
    """带全角引号/尾随逗号的报文仍解析成功。"""
    token, _ = _login(client, "farmer-parse-05")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0005")

    resp = client.post(
        _PARSE_URL, json={"deviceId": device_id, "payload": "{“air_temp”: 26.5,}"}, headers=_headers(token)
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["parsed"] is True
    assert data["fields"][0]["rawField"] == "air_temp"


def test_parse_sample_unknown_device_returns_404(client, engine) -> None:
    """设备不存在或不属于本农户：404（不区分"不存在"与"不属于你"）。"""
    token, _ = _login(client, "farmer-parse-06")

    resp = client.post(_PARSE_URL, json={"deviceId": "dev_not_mine", "payload": "{}"}, headers=_headers(token))

    assert resp.status_code == 404


def test_parse_sample_other_user_device_returns_404(client, engine) -> None:
    """他人设备：404，且不泄露设备存在性。"""
    token_a, _ = _login(client, "farmer-parse-07a")
    token_b, _ = _login(client, "farmer-parse-07b")
    device_id = _bind_and_get_device(client, engine, token_a, "HN-PARSE-0007")

    resp = client.post(_PARSE_URL, json={"deviceId": device_id, "payload": "{}"}, headers=_headers(token_b))

    assert resp.status_code == 404


def test_parse_sample_without_token_returns_401(client, engine) -> None:
    resp = client.post(_PARSE_URL, json={"deviceId": "x", "payload": "{}"})
    assert resp.status_code == 401


# ---- 接口层：模板路径 ----


def test_parse_sample_template_hit_confidence_one(client, engine) -> None:
    """设备模板覆盖报文时走模板路径：所有字段 `confidence == 1.0` 且与模板一致。"""
    _seed_templates(engine)
    token, _ = _login(client, "farmer-parse-08")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0008")

    resp = client.post(
        _PARSE_URL,
        json={"deviceId": device_id, "payload": '{"data": {"air_temp": 26.5, "air_humi": 78}}'},
        headers=_headers(token),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["parsed"] is True
    assert all(f["confidence"] == 1.0 for f in data["fields"])


def test_parse_sample_template_not_covering_falls_back(client, engine) -> None:
    """设备有模板但报文字段不在模板内：退回启发式（`confidence < 1.0`）。"""
    _seed_templates(engine)
    token, _ = _login(client, "farmer-parse-09")
    device_id = _bind_and_get_device(client, engine, token, "HN-PARSE-0009")

    resp = client.post(
        _PARSE_URL,
        json={"deviceId": device_id, "payload": '{"t1": 26.3}'},
        headers=_headers(token),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["parsed"] is True
    assert all(f["confidence"] < 1.0 for f in data["fields"])


def test_parse_sample_rejects_extra_field(client, engine) -> None:
    """`extra="forbid"`：请求体多带字段返回 422。"""
    token, _ = _login(client, "farmer-parse-10")
    resp = client.post(_PARSE_URL, json={"deviceId": "x", "payload": "{}", "extra": 1}, headers=_headers(token))
    assert resp.status_code == 422
