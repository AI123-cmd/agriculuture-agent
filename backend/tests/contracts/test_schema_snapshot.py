"""T6-05：契约字段清单快照，防字段被悄悄删改。

契约的破坏是渐进式的：某次重构把 `EvidenceItem.effective_date` 删掉，没人报错，直到某个
历史详情页渲染不出有效期才发现。本模块把每个契约的**必填字段集**与**可选字段集**写成快照
（取自技术方案附录 A.3 至 A.5 与 A.6 的九个模型），把"删字段"从无声动作变成必须改测试的
显式动作；新增字段同样要同步快照，避免"悄悄加字段"绕过评审。

快照口径与实现对齐：`model_fields` 里 `is_required()` 为真的进 `required`，其余进 `optional`
（有默认值的都算可选）。因此本模块只锁字段名，不锁默认值——默认值是行为，由各契约自身的
用例负责。

另有一条前置断言：全部契约的 `extra` 策略都必须是 `forbid`。若某个契约被改成默认（`ignore`），
模型多吐一个字段就会被静默吞掉，快照虽然仍通过，但"字段清单"已不再是对外承诺的边界。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from app.agent.contracts import (
    DiagnosisResult,
    EvidenceSet,
    GateResult,
    InterviewBatch,
    InterviewQuestion,
    ObservationResult,
    Prescription,
    ReportDecision,
    TriageResult,
)

CONTRACTS: dict[str, type[BaseModel]] = {
    "TriageResult": TriageResult,
    "ObservationResult": ObservationResult,
    "InterviewQuestion": InterviewQuestion,
    "InterviewBatch": InterviewBatch,
    "EvidenceSet": EvidenceSet,
    "DiagnosisResult": DiagnosisResult,
    "Prescription": Prescription,
    "GateResult": GateResult,
    "ReportDecision": ReportDecision,
}

# 字段快照：required 为无默认值的必填字段，optional 为有默认值的字段。
EXPECTED_FIELDS: dict[str, dict[str, set[str]]] = {
    "TriageResult": {
        "required": {"process_id", "object_confirmed", "object_label", "slot_gaps", "profile_ready"},
        "optional": {"fallback_reason", "handoff_to_report"},
    },
    "ObservationResult": {
        "required": {"data_available", "env_source"},
        "optional": {
            "readings",
            "image_candidates",
            "unavailable_reason",
            "observed_facts",
            "note",
        },
    },
    "InterviewQuestion": {
        "required": {"slot_key", "text", "why", "answer_type"},
        "optional": {"options"},
    },
    "InterviewBatch": {
        "required": {"questions", "stage_hint"},
        "optional": {"confidence_note"},
    },
    "EvidenceSet": {
        "required": {"query", "hit", "region_level_actual"},
        "optional": {"items", "reranked", "unmatched_reason", "fallback_chain"},
    },
    "DiagnosisResult": {
        "required": {
            "conclusion",
            "confidence_level",
            "basis",
            "uncertainty_source",
            "excluded",
            "terminology_profile",
            "needs_more_info",
            "out_of_scope",
        },
        "optional": {"boundary_reason"},
    },
    "Prescription": {
        "required": {
            "diagnosis_summary",
            "actions",
            "safety_interval_days",
            "max_uses_per_season",
            "pesticide_items",
            "soil_level_actions",
            "followup_plan",
            "facts_provenance",
            "requires_tech_station",
            "detail_terms",
        },
        "optional": set(),
    },
    "GateResult": {
        "required": {"passed"},
        "optional": {
            "violations",
            "checked_rules",
            "process_redlines_loaded",
            "decision",
            "degrade_output",
            "shadowed",
        },
    },
    "ReportDecision": {
        "required": {
            "triggered",
            "matched_signals",
            "trigger_source",
            "mandatory_actions",
            "contacts",
            "stop_consultation",
        },
        "optional": set(),
    },
}


@pytest.mark.parametrize("name,spec", sorted(EXPECTED_FIELDS.items()))
def test_contract_fields_unchanged(name: str, spec: dict[str, set[str]]) -> None:
    """字段名集合冻结：删字段与加字段都要显式改本快照。"""
    model = CONTRACTS[name]
    fields = model.model_fields
    required = {field for field, info in fields.items() if info.is_required()}
    optional = set(fields) - required

    assert required == spec["required"], f"{name} 必填字段发生变化"
    assert optional == spec["optional"], f"{name} 可选字段发生变化"


def test_snapshot_covers_nine_contracts() -> None:
    """验收标准：快照覆盖附录 A.3 至 A.5 与 A.6 的 `ReportDecision`，共九个契约模型。"""
    assert set(EXPECTED_FIELDS) == set(CONTRACTS)
    assert len(EXPECTED_FIELDS) == 9


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_contract_forbids_extra_fields(name: str) -> None:
    """全部契约必须 `extra="forbid"`：多传字段是契约违约，不能被静默忽略。"""
    model = CONTRACTS[name]
    assert model.model_config.get("extra") == "forbid", f"{name} 未禁止额外字段"


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_contract_json_schema_generates(name: str) -> None:
    """结构化输出约束要读 JSON Schema；生成失败会让该智能体的模型路由直接不可用。"""
    model = CONTRACTS[name]
    schema: dict[str, Any] = model.model_json_schema()
    properties = schema.get("properties") or schema["$defs"][name]["properties"]
    assert set(properties) == set(EXPECTED_FIELDS[name]["required"]) | set(EXPECTED_FIELDS[name]["optional"])
