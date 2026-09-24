"""T2-01：核心枚举的取值稳定性与解析失败行为。

枚举是跨层契约的取值来源（技术方案 5.2「枚举封闭」），因此这里断言的重点
不是"能构造"，而是三条容易被后续任务破坏的性质：
1. 取值集合冻结——多一个少一个都会让 YAML、JSONB 与模型输出对不上；
2. 解析失败必须抛 `EnumParseError`——静默回落会把"配置写错"变成"跑错流程"；
3. `SOURCE_PRIORITY` 的大小关系是黑板覆盖规则的判据（技术方案 6.3）。
"""

from enum import Enum

import pytest

from app.agent.state import (
    SOURCE_PRIORITY,
    AnswerType,
    ConfidenceLevel,
    ConsultStage,
    EvidenceKind,
    GateDecision,
    ObjectType,
    PanelResolution,
    ProcessId,
    SlotGroup,
    SlotOwner,
    SlotSource,
    SlotStatus,
    TaskType,
    TriggerSource,
    ViolationSeverity,
    parse_process_id,
    parse_stage,
)
from app.core.exceptions import EnumParseError


def test_enum_counts_frozen() -> None:
    """七条流程、十一个环节的取值数量固定，是验收标准里的 `7 11`。"""
    assert len(ProcessId) == 7
    assert len(ConsultStage) == 11


def test_process_id_values() -> None:
    assert {p.value for p in ProcessId} == {
        "rice",
        "tea",
        "rapeseed",
        "chili",
        "weed",
        "pig",
        "general",
    }


def test_consult_stage_values() -> None:
    assert {s.value for s in ConsultStage} == {
        "intake",
        "triage",
        "observe",
        "interview",
        "verify",
        "judge",
        "prescribe",
        "gate",
        "followup",
        "handoff",
        "blocked",
    }


def test_all_enums_are_str_mixed() -> None:
    """全部枚举必须混入 `str`，否则 YAML/JSONB 序列化边界上要反复做类型转换。"""
    for enum_cls in (
        ProcessId,
        ConsultStage,
        SlotStatus,
        SlotSource,
        ConfidenceLevel,
        ViolationSeverity,
        GateDecision,
        TriggerSource,
        PanelResolution,
        SlotGroup,
        SlotOwner,
        EvidenceKind,
        AnswerType,
        ObjectType,
        TaskType,
    ):
        assert issubclass(enum_cls, str), enum_cls
        assert issubclass(enum_cls, Enum), enum_cls


def test_str_mixed_equality() -> None:
    """`ProcessId("rice") is ProcessId.RICE` 成立，且与裸字符串相等。"""
    assert ProcessId("rice") is ProcessId.RICE
    assert ProcessId.RICE == "rice"
    assert ConsultStage("blocked") is ConsultStage.BLOCKED


def test_slot_group_uses_chinese_values() -> None:
    """`SlotGroup` 是配置里给人看的分类，取值必须是中文（技术方案 9.1）。"""
    assert SlotGroup.MANAGEMENT.value == "管理"
    assert SlotGroup.PEST.value == "病虫情"


def test_slot_owner_uses_english_values() -> None:
    """`SlotOwner` 是代码标识，出现在写入方判断中，取值必须是英文。"""
    assert {o.value for o in SlotOwner} == {"field_observer", "interviewer", "profile_loader"}


def test_source_priority_ordering() -> None:
    """优先级表覆盖全部来源且严格递增：高优先级数值小，可覆盖低优先级。"""
    assert set(SOURCE_PRIORITY) == set(SlotSource)
    assert SOURCE_PRIORITY[SlotSource.PROFILE] < SOURCE_PRIORITY[SlotSource.INFERRED]
    ordered = [SOURCE_PRIORITY[s] for s in SlotSource]
    assert ordered == sorted(ordered)
    assert len(set(ordered)) == len(ordered)


def test_parse_stage_accepts_valid_value() -> None:
    assert parse_stage("triage") is ConsultStage.TRIAGE
    assert parse_stage(ConsultStage.GATE.value) is ConsultStage.GATE


def test_parse_stage_rejects_unknown_value() -> None:
    with pytest.raises(EnumParseError) as exc:
        parse_stage("unknown_stage")
    assert "unknown_stage" in str(exc.value)


def test_parse_process_id_accepts_valid_value() -> None:
    assert parse_process_id("tea") is ProcessId.TEA


def test_parse_process_id_rejects_unregistered_value() -> None:
    """未登记的流程名不得静默回落到 general，必须抛异常。"""
    with pytest.raises(EnumParseError) as exc:
        parse_process_id("bees")
    assert "bees" in str(exc.value)


def test_enum_parse_error_is_app_exception() -> None:
    """继承 `AppException`，才能被全局 handler 转成响应而不是 500 裸栈。"""
    from app.core.exceptions import AppException

    assert issubclass(EnumParseError, AppException)
