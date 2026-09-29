"""T2-36：`R-REGISTERED-CROP` 登记作物匹配。

本规则只实现茶流程三重条件的第一重——"已登记"。它按 `process_id` 取植保口径的作物名，
再核对知识条目的 `registered_crops`。杂草、养猪、通用三条流程不做登记匹配（`CROP_MAPPING`
未覆盖即豁免），这是刻意的：登记匹配在那三条流程内无定义或会造成大面积误杀。
"""

import inspect
from pathlib import Path

from app.gate.enums import GateRuleId, ProcessId
from app.gate.ports import KnowledgeFacts
from app.gate.rules.base import build_rules
from app.gate.rules.registered_crop import RegisteredCropRule
from tests.gate.fixtures.factories import gate_context, pesticide_item, prescription
from tests.gate.fixtures.fake_knowledge import FakeEntry, FakeKnowledge

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "registered_crop.py"


def _run(ctx):
    return RegisteredCropRule().check(ctx)


# ---- 正例 ----


def test_tea_process_registered_crop_passes() -> None:
    """正例 1：茶流程，条目登记作物含"茶"，返回空列表。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", registered_crops=("茶", "茶树"))}
    )
    ctx = gate_context(
        process_id=ProcessId.TEA,
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )

    assert _run(ctx) == []


def test_weed_process_exempt() -> None:
    """正例 2：杂草流程不在 `CROP_MAPPING` 内，任何条目均返回空列表。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="某除草剂", registered_crops=())})
    ctx = gate_context(
        process_id=ProcessId.WEED,
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="某除草剂")]),
    )

    assert _run(ctx) == []


# ---- 反例 ----


def test_tea_not_registered_blocked() -> None:
    """反例 1：茶流程条目只登记了水稻小麦，返回 1 条 block 违规并带出登记作物清单。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", registered_crops=("水稻", "小麦"))}
    )
    ctx = gate_context(
        process_id=ProcessId.TEA,
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "pesticide_items[K-1001]"
    assert "未在茶上登记" in violation.message
    assert "登记作物：水稻、小麦" in violation.message


def test_empty_registered_crops_reports_none() -> None:
    """反例 2：水稻流程条目 `registered_crops=()`，提示语用"无"与"未登记"区分开。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", registered_crops=())}
    )
    ctx = gate_context(
        process_id=ProcessId.RICE,
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert "登记作物：无" in violation.message


# ---- CROP_MAPPING 与装配 ----


def test_crop_mapping_covers_exactly_four_crop_processes() -> None:
    """映射键恰为四条作物流程；杂草/通用/养猪不在其中（各自另有处置路径）。"""
    assert set(RegisteredCropRule.CROP_MAPPING) == {
        ProcessId.TEA,
        ProcessId.RICE,
        ProcessId.CHILI,
        ProcessId.RAPESEED,
    }
    assert ProcessId.GENERAL not in RegisteredCropRule.CROP_MAPPING
    assert ProcessId.PIG not in RegisteredCropRule.CROP_MAPPING


def test_rule_metadata_and_registration() -> None:
    assert RegisteredCropRule.rule_id == GateRuleId.REGISTERED_CROP.value
    assert RegisteredCropRule.severity == "block"

    (rule,) = build_rules([GateRuleId.REGISTERED_CROP.value])

    assert isinstance(rule, RegisteredCropRule)


def test_protocol_declares_get_registered_crops_returning_tuple() -> None:
    """协议声明 `get_registered_crops` 且返回 `tuple[str, ...]`，假实现与协议结构一致。"""
    assert hasattr(KnowledgeFacts, "get_registered_crops")

    signature = inspect.signature(KnowledgeFacts.get_registered_crops)
    assert str(signature.return_annotation) == "tuple[str, ...]"

    fake_signature = inspect.signature(FakeKnowledge.get_registered_crops)
    assert str(fake_signature.return_annotation) == "tuple[str, ...]"


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
