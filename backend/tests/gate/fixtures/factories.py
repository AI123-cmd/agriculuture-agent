"""闸门规则测试的处方与用药史构造器（T2-37）。

规则读的都是 `GatePrescription` / `MedicationRecord` 这两个只读镜像，因此这里提供
构造器：默认给一条"全部合法"的药剂与一条带停止条件的动作，各用例只覆盖自己关心的
字段，不重复搭脚手架。
"""

from datetime import date

from app.gate.contracts import (
    GateAction,
    GateContext,
    GatePayload,
    GatePesticideItem,
    GatePrescription,
    MedicationRecord,
)
from app.gate.enums import ProcessId

from .fake_knowledge import FakeKnowledge

DEFAULT_ITEM = {
    "knowledge_id": "K-1001",
    "name": "吡虫啉",
    "dosage_min": 15.0,
    "dosage_max": 20.0,
    "dosage_unit": "克",
    "safety_interval_days": 7,
    "max_uses_per_season": 2,
}


def pesticide_item(**overrides) -> GatePesticideItem:
    """构造一条药剂条目。默认字段与 `FakeEntry` 的默认条目一致。"""
    data = {**DEFAULT_ITEM, **overrides}
    return GatePesticideItem(**data)


def medication_record(
    *,
    pesticide_name: str = "吡虫啉",
    used_on: date,
    knowledge_id: str | None = "K-1001",
) -> MedicationRecord:
    """构造一条用药史记录。"""
    return MedicationRecord(knowledge_id=knowledge_id, pesticide_name=pesticide_name, used_on=used_on)


def default_action() -> GateAction:
    return GateAction(
        order=1,
        action="建议喷施吡虫啉",
        timing="晴天傍晚",
        method="叶面均匀喷雾",
        cautions=["避免高温施药"],
        stop_conditions=["出现药害斑立即停用"],
    )


def prescription(
    *,
    items: list[GatePesticideItem] | None = None,
    actions: list[GateAction] | None = None,
    safety_interval_days: int | None = 7,
    max_uses_per_season: int | None = None,
    facts_provenance: dict[str, str] | None = None,
    detail_terms: dict[str, str] | None = None,
) -> GatePrescription:
    """构造只读处方镜像。默认给一条合法药剂与一条带停止条件的动作。"""
    if items is None:
        items = [pesticide_item()]
    if actions is None:
        actions = [default_action()]
    return GatePrescription(
        diagnosis_summary="示例诊断",
        actions=actions,
        safety_interval_days=safety_interval_days,
        max_uses_per_season=max_uses_per_season,
        pesticide_items=items,
        facts_provenance=facts_provenance if facts_provenance is not None else {},
        detail_terms=detail_terms if detail_terms is not None else {},
    )


def gate_context(**overrides) -> GateContext:
    """构造一个默认"全部合法"的 `GateContext`。各规则测试按需覆盖字段。"""
    from .fake_knowledge import loaded_redlines

    base: dict = {
        "session_id": "sess-0001",
        "user_id": "user-0001",
        "process_id": ProcessId.RICE,
        "config_version": 1,
        "prescription": prescription(),
        "payload": GatePayload(rendered_text="建议在傍晚喷施，若出现药害立即停用。"),
        "redlines": loaded_redlines(),
        "slots": {},
        "knowledge": FakeKnowledge(),
        "medication_history": [],
        "region_code": None,
        "today": date(2025, 6, 1),
        "season_start": None,
        "rule_configs": {},
    }
    base.update(overrides)
    return GateContext(**base)


__all__ = [
    "default_action",
    "gate_context",
    "medication_record",
    "pesticide_item",
    "prescription",
]
