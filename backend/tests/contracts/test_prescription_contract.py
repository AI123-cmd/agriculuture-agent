"""T2-11：处方契约的溯源覆盖与药剂来源约束。

两类反例是重点：数值字段没登记溯源（"模板出事实、模型只改写"失去自动检查）、以及
`stop_conditions` 缺失（农户会一路执行到出现明显损失）。
"""

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    FollowupPlan,
    FollowupTask,
    PesticideItem,
    Prescription,
    PrescriptionAction,
    dump_contract,
    load_contract,
)


def _action(**overrides) -> dict:
    base = {
        "order": 1,
        "action": "喷施推荐药剂",
        "timing": "晴天傍晚",
        "method": "叶面均匀喷雾，重点覆盖叶背",
        "cautions": ["避免高温时段施药"],
        "stop_conditions": ["出现药害斑立即停用并联系农技站"],
    }
    base.update(overrides)
    return base


def _pesticide(**overrides) -> dict:
    base = {
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
    base.update(overrides)
    return base


def _followup(**overrides) -> dict:
    base = {
        "tasks": [
            FollowupTask(due_offset_days=3, kind="effect_check", content_rule="tea.effect_check"),
            FollowupTask(due_offset_days=0, kind="observe"),
        ],
        "note": None,
    }
    base.update(overrides)
    return base


def _prescription(**overrides) -> dict:
    """含两个药剂条目的处方，`facts_provenance` 覆盖 `numeric_paths()` 的全部路径。"""
    base = {
        "diagnosis_summary": "茶小绿叶蝉若虫危害，中等发生程度",
        "actions": [PrescriptionAction(**_action())],
        "safety_interval_days": 7,
        "max_uses_per_season": 2,
        "pesticide_items": [
            PesticideItem(**_pesticide()),
            PesticideItem(
                **_pesticide(
                    knowledge_id="kb_tea_pest_002",
                    name="示例药剂B",
                    dosage_min=10.0,
                    dosage_max=None,
                    safety_interval_days=None,
                    max_uses_per_season=None,
                )
            ),
        ],
        "soil_level_actions": [],
        "followup_plan": FollowupPlan(**_followup()),
        "facts_provenance": {
            "safety_interval_days": "knowledge:kb_tea_pest_001",
            "max_uses_per_season": "knowledge:kb_tea_pest_001:field:max_uses_per_season",
            "pesticide_items[0].dosage_min": "knowledge:kb_tea_pest_001:field:dosage_min",
            "pesticide_items[0].dosage_max": "knowledge:kb_tea_pest_001:field:dosage_max",
            "pesticide_items[0].safety_interval_days": "knowledge:kb_tea_pest_001:field:safety_interval_days",
            "pesticide_items[0].max_uses_per_season": ("knowledge:kb_tea_pest_001:field:max_uses_per_season"),
            "pesticide_items[1].dosage_min": "knowledge:kb_tea_pest_002:field:dosage_min",
        },
        "requires_tech_station": False,
        "detail_terms": {"茶小绿叶蝉": "吸汁液的小绿虫"},
    }
    base.update(overrides)
    return base


def test_valid_prescription_passes() -> None:
    result = Prescription(**_prescription())
    assert len(result.pesticide_items) == 2
    assert result.requires_tech_station is False


def test_numeric_paths_include_pesticide_field_paths() -> None:
    """路径形如 `pesticide_items[0].dosage_min`，闸门（T2-41）的反向扫描按同一形态取值。"""
    paths = Prescription(**_prescription()).numeric_paths()
    assert "pesticide_items[0].dosage_min" in paths
    assert "pesticide_items[1].dosage_min" in paths
    assert "safety_interval_days" in paths
    assert "max_uses_per_season" in paths


def test_numeric_paths_skip_none_fields() -> None:
    """取值为 `None` 的字段不进路径清单：没有数字就没有需要溯源的对象。"""
    paths = Prescription(**_prescription()).numeric_paths()
    assert "pesticide_items[1].dosage_max" not in paths
    assert "pesticide_items[1].safety_interval_days" not in paths


def test_missing_provenance_raises() -> None:
    """数值字段未登记溯源即抛错——这是"每个数字都有出处"的自动检查。"""
    provenance = dict(_prescription()["facts_provenance"])
    provenance.pop("pesticide_items[0].dosage_min")
    with pytest.raises(ValidationError, match="数值字段缺少溯源"):
        Prescription(**_prescription(facts_provenance=provenance))


def test_missing_provenance_error_names_offending_path() -> None:
    provenance = dict(_prescription()["facts_provenance"])
    provenance.pop("pesticide_items[1].dosage_min")
    with pytest.raises(ValidationError, match=r"pesticide_items\[1\]\.dosage_min"):
        Prescription(**_prescription(facts_provenance=provenance))


def test_no_pesticide_means_no_pesticide_numeric_paths() -> None:
    """无药剂时不允许出现药剂数值：路径清单为空，不需要额外的分支判断来保证这一点。"""
    result = Prescription(
        **_prescription(
            pesticide_items=[],
            safety_interval_days=None,
            max_uses_per_season=None,
            facts_provenance={},
        )
    )
    assert result.numeric_paths() == []
    assert result.pesticide_items == []


def test_no_pesticide_but_stray_numeric_path_raises() -> None:
    """没有药剂却写下安全间隔期时，该数值同样必须有来源，不能被当作"反正没药剂"放过。"""
    with pytest.raises(ValidationError, match="数值字段缺少溯源"):
        Prescription(
            **_prescription(
                pesticide_items=[],
                safety_interval_days=7,
                max_uses_per_season=None,
                facts_provenance={},
            )
        )


def test_pesticide_without_interval_is_not_blocked_by_contract() -> None:
    """涉及用药却没给安全间隔期时契约不阻断，交由闸门 `R-INTERVAL` 拦截并降级。

    若契约抛错，处方节点会重试且可能反复失败，而正确行为是让闸门给出 `degrade` 输出。
    """
    result = Prescription(
        **_prescription(
            safety_interval_days=None,
            facts_provenance={
                k: v for k, v in _prescription()["facts_provenance"].items() if k != "safety_interval_days"
            },
        )
    )
    assert result.safety_interval_days is None
    assert len(result.pesticide_items) == 2


def test_action_without_stop_conditions_raises() -> None:
    with pytest.raises(ValidationError, match="stop_conditions"):
        PrescriptionAction(**_action(stop_conditions=[]))


def test_action_requires_all_fields() -> None:
    payload = _action()
    payload.pop("stop_conditions")
    with pytest.raises(ValidationError):
        PrescriptionAction(**payload)


def test_pesticide_requires_knowledge_id() -> None:
    """`knowledge_id` 是闸门 `R-WHITELIST` 的校验依据，缺失则无从核验，因此必填。"""
    payload = _pesticide()
    payload.pop("knowledge_id")
    with pytest.raises(ValidationError):
        PesticideItem(**payload)


def test_pesticide_optional_numbers_default_to_none() -> None:
    item = PesticideItem(knowledge_id="kb_x", name="示例")
    assert item.dosage_min is None
    assert item.dosage_max is None
    assert item.safety_interval_days is None
    assert item.max_uses_per_season is None
    assert item.registered_crops == []


def test_followup_tasks_sorted_ascending() -> None:
    """回访调度不必自己排序，也不必担心乱序导致同一天多次触达农户。"""
    plan = FollowupPlan(**_followup())
    assert [t.due_offset_days for t in plan.tasks] == [0, 3]
    assert plan.tasks[0].kind == "observe"


def test_followup_sorted_in_place_from_unsorted_input() -> None:
    plan = FollowupPlan(
        tasks=[
            FollowupTask(due_offset_days=14, kind="effect_check"),
            FollowupTask(due_offset_days=0, kind="preserve_scene"),
            FollowupTask(due_offset_days=3, kind="risk_check"),
        ]
    )
    assert [t.due_offset_days for t in plan.tasks] == [0, 3, 14]


def test_followup_task_fields_match_spec() -> None:
    assert set(FollowupTask.model_fields) == {
        "due_offset_days",
        "kind",
        "content_rule",
        "ask",
        "window_based",
    }


def test_followup_task_kind_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        FollowupTask(due_offset_days=1, kind="call_farmer")


def test_followup_window_based_default_false() -> None:
    assert FollowupTask(due_offset_days=1, kind="observe").window_based is False


def test_prescription_fields_match_spec() -> None:
    assert set(Prescription.model_fields) == {
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
    }


def test_action_fields_match_spec() -> None:
    assert set(PrescriptionAction.model_fields) == {
        "order",
        "action",
        "timing",
        "method",
        "cautions",
        "stop_conditions",
    }


def test_extra_field_raises() -> None:
    with pytest.raises(ValidationError):
        Prescription(**_prescription(confidence=0.9))


def test_json_schema_generates() -> None:
    """结构化输出约束要读 JSON Schema，生成失败即无法约束模型输出。"""
    schema = Prescription.model_json_schema()
    assert "facts_provenance" in schema["properties"]
    assert "followup_plan" in schema["properties"]


def test_roundtrip_through_state_payload() -> None:
    original = Prescription(**_prescription())
    payload = dump_contract(original)
    assert payload["actions"][0]["order"] == 1
    assert payload["followup_plan"]["tasks"][0]["due_offset_days"] == 0
    assert load_contract(Prescription, payload) == original
