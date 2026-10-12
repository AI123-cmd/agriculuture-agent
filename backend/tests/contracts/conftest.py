"""T6-05：契约测试的合法样例工厂。

本包只测**智能体间契约**（`app/agent/contracts/`），不测**接口出入参**。两者边界必须分清：

- `app/agent/contracts/`：智能体之间的数据形态，用 `BaseModel` + `extra="forbid"` 定义，
  字段缺失、枚举越界、跨字段矛盾在对象构造时即暴露。这是本项的测量对象。
- `app/schemas/`（`ai.py`、`broadcast.py` 等）：HTTP 接口的出入参，其覆盖在集成测试与
  接口改造用例中。若把出入参也塞进 `tests/contracts/`，改一次出参就会让契约快照失败，
  噪声大且掩盖真正的契约回归。

工厂统一返回**可覆盖的 dict**（而非已构造对象），调用方 `Model(**make_x())` 后按需覆写单个
键，既能拿到合法基线，又能精确构造反例。样例均取自技术方案附录 A 的真实形态，字段名一律
以代码现状为准：附录里早期写法的 `TriageResult.confidence`、`Prescription.draft_text`、
`PrescriptionAction.ref_id` / `safety_interval_days`、`InterviewBatch.can_proceed` /
`missing_slots` 在当前契约中都不存在，不得引入。
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

# ---- 分诊（A.3） ----


def make_triage() -> dict[str, Any]:
    """合法的 `TriageResult`：已确认对象、进专属流程。"""
    return {
        "process_id": "tea",
        "object_confirmed": True,
        "object_label": "茶树·2亩",
        "slot_gaps": ["tea_leaf_symptom"],
        "profile_ready": True,
    }


# ---- 现场采集（A.2） ----


def make_observation() -> dict[str, Any]:
    """合法的 `ObservationResult`：有环境数据。"""
    return {
        "data_available": True,
        "env_source": "sensor",
        "readings": [],
        "image_candidates": [],
        "observed_facts": {},
        "note": None,
        "unavailable_reason": None,
    }


# ---- 问诊（A.3） ----


def make_interview_question() -> dict[str, Any]:
    return {
        "slot_key": "tea_leaf_symptom",
        "text": "叶片上是什么样子的斑？",
        "why": "不同病斑形状指向不同病害，先确认样子能少走弯路",
        "answer_type": "single",
        "options": ["圆形褐斑", "不规则黄斑", "说不清"],
    }


def make_interview_batch() -> dict[str, Any]:
    return {"questions": [make_interview_question()], "stage_hint": "正在了解情况 1/3"}


# ---- 证据（A.2） ----


def make_evidence_set() -> dict[str, Any]:
    return {
        "query": {
            "process_id": "tea",
            "keywords": ["茶小绿叶蝉"],
            "region_code": "330100",
            "slot_snapshot": {"tea_leaf_symptom": "叶片卷曲"},
            "kind_filter": ["pest"],
            "top_k": 8,
        },
        "items": [
            {
                "knowledge_id": "kb_tea_001",
                "title": "茶小绿叶蝉防治",
                "content": "若虫期用药效果最好。",
                "process_id": "tea",
                "region_scope": "浙江省杭州市",
                "effective_date": "2025-03-01",
                "kind": "pest",
                "score": 0.87,
            }
        ],
        "hit": True,
        "region_level_actual": "county",
        "fallback_chain": ["county:330100"],
        "reranked": False,
    }


# ---- 判定（A.4） ----


def make_diagnosis() -> dict[str, Any]:
    return {
        "conclusion": "茶小绿叶蝉若虫危害",
        "confidence_level": "likely",
        "basis": ["叶片出现均匀失绿斑点，符合若虫刺吸特征"],
        "uncertainty_source": [],
        "excluded": ["茶炭疽病"],
        "terminology_profile": "tea",
        "needs_more_info": [],
        "out_of_scope": False,
        "boundary_reason": None,
    }


# ---- 处方（A.4） ----


def make_prescription() -> dict[str, Any]:
    """合法处方，`facts_provenance` 覆盖 `numeric_paths()` 的全部数值路径。"""
    return {
        "diagnosis_summary": "茶小绿叶蝉若虫危害，中等发生程度",
        "actions": [
            {
                "order": 1,
                "action": "喷施推荐药剂",
                "timing": "晴天傍晚",
                "method": "叶面均匀喷雾，重点覆盖叶背",
                "cautions": ["避免高温时段施药"],
                "stop_conditions": ["出现药害斑立即停用并联系农技站"],
            }
        ],
        "safety_interval_days": 7,
        "max_uses_per_season": 2,
        "pesticide_items": [
            {
                "knowledge_id": "kb_tea_pest_001",
                "name": "示例药剂A",
                "dosage_min": 15.0,
                "dosage_max": 20.0,
                "dosage_unit": "ml/亩",
                "safety_interval_days": 7,
                "max_uses_per_season": 2,
                "registered_crops": ["茶"],
                "forbidden_combinations": [],
                "source_note": "知识库条目标注：花期禁用",
            }
        ],
        "soil_level_actions": [],
        "followup_plan": {"tasks": [], "note": None},
        "facts_provenance": {
            "safety_interval_days": "knowledge:kb_tea_pest_001",
            "max_uses_per_season": "knowledge:kb_tea_pest_001",
            "pesticide_items[0].dosage_min": "knowledge:kb_tea_pest_001:field:dosage_min",
            "pesticide_items[0].dosage_max": "knowledge:kb_tea_pest_001:field:dosage_max",
            "pesticide_items[0].safety_interval_days": "knowledge:kb_tea_pest_001",
            "pesticide_items[0].max_uses_per_season": "knowledge:kb_tea_pest_001",
        },
        "requires_tech_station": False,
        "detail_terms": {"茶小绿叶蝉": "吸汁液的小绿虫"},
    }


# ---- 闸门（A.5） ----


def make_gate_result() -> dict[str, Any]:
    """合法闸门结果：放行且无违规。"""
    return {
        "passed": True,
        "violations": [],
        "checked_rules": ["R-REDLINE-LOAD", "R-WHITELIST"],
        "process_redlines_loaded": True,
        "decision": "allow",
        "degrade_output": None,
    }


# ---- 报告守卫（A.6） ----


def make_report_decision() -> dict[str, Any]:
    """合法报告决定：已触发，给得出可执行内容。"""
    return {
        "triggered": True,
        "matched_signals": ["群体性死亡"],
        "trigger_source": "mass_death",
        "mandatory_actions": ["立即隔离发病个体", "对栏舍消毒"],
        "contacts": [],
        "stop_consultation": True,
    }


_FACTORIES: dict[str, Callable[[], dict[str, Any]]] = {
    "triage": make_triage,
    "observation": make_observation,
    "interview_question": make_interview_question,
    "interview_batch": make_interview_batch,
    "evidence_set": make_evidence_set,
    "diagnosis": make_diagnosis,
    "prescription": make_prescription,
    "gate_result": make_gate_result,
    "report_decision": make_report_decision,
}


@pytest.fixture(scope="session")
def samples() -> dict[str, Callable[[], dict[str, Any]]]:
    """按名取合法样例工厂：`samples["prescription"]()` 返回一份可覆写的 dict。"""
    return _FACTORIES
