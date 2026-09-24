"""T2-09：现场采集契约的正反例与两条校验器。

重点断言两类反例——它们拦的都是"能构造出来但界面会撒谎"的输出：未标原因的数据不可用、
以及无环境数据时缺少面向农户的说明（产品说明书 4.2.3 功能点 6）。
"""

import pytest
from pydantic import ValidationError

from app.agent.contracts import EnvReading, ImageCandidate, ObservationResult


def _obs(**overrides) -> dict:
    base = {
        "data_available": True,
        "env_source": "sensor",
    }
    base.update(overrides)
    return base


def test_minimal_available_result_passes() -> None:
    result = ObservationResult(**_obs())
    assert result.readings == []
    assert result.image_candidates == []
    assert result.observed_facts == {}


def test_unavailable_without_reason_raises() -> None:
    """拿不到数据时必须说明原因，否则运维与农户看到的是同一句"暂无数据"。"""
    with pytest.raises(ValidationError, match="unavailable_reason"):
        ObservationResult(**_obs(data_available=False, env_source="none", note="暂无环境数据"))


def test_unavailable_with_reason_passes() -> None:
    result = ObservationResult(
        **_obs(data_available=False, env_source="none", unavailable_reason="该地块未安装传感器", note="暂无环境数据")
    )
    assert result.data_available is False
    assert result.unavailable_reason == "该地块未安装传感器"


def test_env_source_none_requires_note() -> None:
    with pytest.raises(ValidationError, match="note"):
        ObservationResult(**_obs(env_source="none"))


def test_env_source_sensor_stale_requires_note() -> None:
    with pytest.raises(ValidationError, match="note"):
        ObservationResult(**_obs(env_source="sensor_stale"))


def test_sensor_stale_with_note_passes() -> None:
    """传感器数据过期不阻断问诊，但界面必须标注判断依据。"""
    result = ObservationResult(**_obs(env_source="sensor_stale", note="传感器数据超过 2 小时未更新，判断基于你的描述"))
    assert result.note is not None


def test_readings_distinguish_suspect_from_missing() -> None:
    """读到但可疑（有值 + suspect）与根本没读到（无值 + missing）是两种形态。"""
    suspect = EnvReading(metric="soil_moisture", value=82.0, unit="%", source="sensor", quality="suspect")
    missing = EnvReading(metric="soil_moisture", value=None, unit="%", source="sensor", quality="missing")
    assert suspect.value is not None
    assert missing.value is None
    assert missing.quality == "missing"


def test_reading_rejects_unknown_metric() -> None:
    """枚举封闭：读数指标是封闭集合，新增指标必须同时改契约（否则闸门看不到它）。"""
    with pytest.raises(ValidationError):
        EnvReading(metric="leaf_wetness", value=1.0, unit="-", source="sensor")


def test_reading_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        EnvReading(metric="air_temp", value=28.0, unit="℃", source="guess")


def test_reading_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        EnvReading(metric="air_temp", value=28.0, unit="℃", source="sensor", provenance={})


def test_image_candidate_part_optional() -> None:
    cand = ImageCandidate(label="茶小绿叶蝉", score=0.72)
    assert cand.part is None
    assert ImageCandidate(label="茶小绿叶蝉", score=0.72, part="叶片").part == "叶片"


def test_observed_facts_are_colloquial_dict() -> None:
    """`observed_facts` 键为事实名、值为口语化描述，进入 `ConsultationState.observations`。"""
    result = ObservationResult(**_obs(observed_facts={"rain_last_3d": "近三天有连续降雨"}))
    assert result.observed_facts["rain_last_3d"] == "近三天有连续降雨"


def test_observation_fields_match_spec() -> None:
    assert set(ObservationResult.model_fields) == {
        "readings",
        "image_candidates",
        "data_available",
        "unavailable_reason",
        "env_source",
        "observed_facts",
        "note",
    }


def test_env_reading_fields_match_spec() -> None:
    assert set(EnvReading.model_fields) == {
        "metric",
        "value",
        "unit",
        "source",
        "quality",
        "observed_at",
        "device_sn",
    }


def test_json_schema_generates() -> None:
    """结构化输出约束要用它，生成失败会让现场采集的模型路由直接不可用。"""
    schema = ObservationResult.model_json_schema()
    assert "env_source" in schema["properties"]


def test_roundtrip_through_state_payload() -> None:
    from app.agent.contracts import dump_contract, load_contract

    original = ObservationResult(**_obs(observed_facts={"a": "b"}, env_source="weather"))
    restored = load_contract(ObservationResult, dump_contract(original))
    assert restored == original
