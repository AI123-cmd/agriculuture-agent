"""T2-12：报告守卫契约的触发内容与中止问诊的双向约束。

报告守卫与安全闸门是仅有的两个 fail-closed 组件，其余智能体降级仍服务。因此反例集中在
"触发了却给不出可执行内容"与"没触发却中止了问诊"两类——两者都会让农户拿到一条无法照做
或无故中断的结论。
"""

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    ContactInfo,
    ReportDecision,
    ReportSignal,
    dump_contract,
    load_contract,
)


def _contact(**overrides) -> dict:
    base = {
        "kind": "vet_station",
        "name": "XX镇兽医站",
        "phone": "0571-00000000",
        "region_code": "330100",
        "note": "工作日 8:30-17:00",
    }
    base.update(overrides)
    return base


def _triggered(**overrides) -> dict:
    base = {
        "triggered": True,
        "matched_signals": ["群体性死亡"],
        "trigger_source": "mass_death",
        "mandatory_actions": ["立即隔离发病个体", "对栏舍消毒", "向当地兽医站报告"],
        "contacts": [ContactInfo(**_contact())],
        "stop_consultation": True,
    }
    base.update(overrides)
    return base


def test_triggered_decision_passes() -> None:
    result = ReportDecision(**_triggered())
    assert result.triggered is True
    assert result.stop_consultation is True
    assert len(result.contacts) == 1


def test_single_signal_triggers_without_deaths() -> None:
    """产品说明书 5.7：不是只有群死群伤才需报告，单个识别信号命中即触发。

    这条门槛必须低：误报的代价是农户多打一个电话，漏报的代价是疫病扩散。
    """
    result = ReportDecision(
        **_triggered(
            matched_signals=["个别猪只皮肤出现出血点"],
            trigger_source="signal",
            mandatory_actions=["隔离该个体并联系兽医站"],
        )
    )
    assert result.trigger_source == "signal"
    assert result.stop_consultation is True


def test_triggered_without_signals_raises() -> None:
    with pytest.raises(ValidationError, match="识别信号"):
        ReportDecision(**_triggered(matched_signals=[]))


def test_triggered_without_actions_raises() -> None:
    with pytest.raises(ValidationError, match="强制动作"):
        ReportDecision(**_triggered(mandatory_actions=[]))


def test_triggered_with_source_none_raises() -> None:
    with pytest.raises(ValidationError, match="trigger_source"):
        ReportDecision(**_triggered(trigger_source="none"))


def test_triggered_may_have_empty_contacts() -> None:
    """本地联系入口可能尚未配置：`contacts` 为空时由 T2-22 记录告警，而不是让最需要输出
    的报告结论构造失败。"""
    result = ReportDecision(**_triggered(contacts=[]))
    assert result.contacts == []


def test_not_triggered_passes() -> None:
    result = ReportDecision(
        triggered=False,
        matched_signals=[],
        trigger_source="none",
        mandatory_actions=[],
        contacts=[],
        stop_consultation=False,
    )
    assert result.triggered is False
    assert result.stop_consultation is False


def test_not_triggered_cannot_stop_consultation() -> None:
    with pytest.raises(ValidationError, match="中止问诊"):
        ReportDecision(
            triggered=False,
            matched_signals=[],
            trigger_source="none",
            mandatory_actions=[],
            contacts=[],
            stop_consultation=True,
        )


def test_not_triggered_cannot_have_source() -> None:
    """未触发却记了来源，会在审计里留下自相矛盾的一条记录。"""
    with pytest.raises(ValidationError, match="trigger_source"):
        ReportDecision(
            triggered=False,
            matched_signals=[],
            trigger_source="signal",
            mandatory_actions=[],
            contacts=[],
            stop_consultation=False,
        )


@pytest.mark.parametrize("source", ["signal", "group", "mass_death", "none"])
def test_trigger_source_literal_values(source: str) -> None:
    """四值全集：`signal` 是低门槛分支，`none` 只在未触发时合法。"""
    literal_values = set(
        ReportDecision.model_fields["trigger_source"].annotation.__args__  # type: ignore[union-attr]
    )
    assert literal_values == {"signal", "group", "mass_death", "none"}
    assert source in literal_values


def test_unknown_trigger_source_raises() -> None:
    with pytest.raises(ValidationError):
        ReportDecision(**_triggered(trigger_source="suspected"))


def test_triggered_requires_all_fields_declared() -> None:
    assert set(ReportDecision.model_fields) == {
        "triggered",
        "matched_signals",
        "trigger_source",
        "mandatory_actions",
        "contacts",
        "stop_consultation",
    }


def test_triggered_extra_field_raises() -> None:
    with pytest.raises(ValidationError):
        ReportDecision(**_triggered(severity="high"))


@pytest.mark.parametrize("kind", ["vet_station", "agri_station", "authority", "hotline"])
def test_contact_kinds_accepted(kind: str) -> None:
    """养猪流程的法定报告对象与种植流程的农技站不是同一类，前端按类别分组展示。"""
    contact = ContactInfo(**_contact(kind=kind))
    assert contact.kind == kind


def test_contact_unknown_kind_raises() -> None:
    with pytest.raises(ValidationError):
        ContactInfo(**_contact(kind="police"))


def test_contact_phone_optional_but_kind_required() -> None:
    """地图与地址尚未接入时，至少能告诉农户"该联系哪个机构"。"""
    contact = ContactInfo(kind="agri_station", name="XX镇农技站")
    assert contact.phone is None
    assert contact.region_code is None
    assert contact.note is None


def test_contact_name_required() -> None:
    with pytest.raises(ValidationError):
        ContactInfo(kind="hotline")


@pytest.mark.parametrize("severity", ["signal", "group", "mass_death"])
def test_report_signal_severity_values(severity: str) -> None:
    signal = ReportSignal(
        signal_id="pig_mass_death",
        keywords=["死了好几头", "集体死亡"],
        slot_key="pig_death_count",
        severity=severity,
        description="出现群体性死亡",
    )
    assert signal.severity == severity


def test_report_signal_rejects_none_severity() -> None:
    """`ReportSignal` 描述的是信号本身，不存在 `none` 档——那是未触发的状态。"""
    with pytest.raises(ValidationError):
        ReportSignal(
            signal_id="x",
            keywords=[],
            severity="none",
            description="未触发",
        )


def test_report_signal_fields_match_spec() -> None:
    assert set(ReportSignal.model_fields) == {
        "signal_id",
        "keywords",
        "slot_key",
        "severity",
        "description",
    }


def test_report_signal_slot_key_defaults_none() -> None:
    signal = ReportSignal(
        signal_id="pig_skin_lesion",
        keywords=["皮肤出血点"],
        severity="signal",
        description="个别猪只皮肤出现出血点",
    )
    assert signal.slot_key is None


def test_json_schema_generates() -> None:
    schema = ReportDecision.model_json_schema()
    assert "trigger_source" in schema["properties"]
    assert "stop_consultation" in schema["properties"]
    assert "contacts" in schema["properties"]


def test_roundtrip_through_state_payload() -> None:
    original = ReportDecision(**_triggered())
    payload = dump_contract(original)
    assert payload["trigger_source"] == "mass_death"
    assert payload["contacts"][0]["kind"] == "vet_station"
    assert load_contract(ReportDecision, payload) == original
