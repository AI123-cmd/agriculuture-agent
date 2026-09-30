"""T2-02：流程配置模型的正例与反例。

模型层的价值在于"配置写错就在加载阶段报错"，因此这里覆盖的重点是那些
容易静默失效的写法：拼错的字段名（`extra="forbid"`）、缺决定性特征的
易混淆项（`ConfusablePair` 校验器）、以及配置对象被中途改写（`frozen=True`）。
"""

import pytest
from pydantic import ValidationError

from app.process_config.models import ProcessConfig


def _minimal(**overrides) -> dict:
    """一份合法的最小配置，逐项覆盖时只改关心的字段。"""
    base = {
        "process_id": "rice",
        "version": 1,
        "display_name": "水稻",
        "role_definition": "水稻种植顾问",
        "required_slots": [
            {
                "key": "rice_symptom_part",
                "label": "受害部位",
                "display_label": "水稻哪个部位出问题了",
                "group": "病虫情",
                "owner": "interviewer",
            }
        ],
        "redlines": [
            {
                "id": "RICE-01",
                "desc": "不得推荐未在水稻上登记的农药",
                "rule": "R-REGISTERED-CROP",
                "severity": "block",
            }
        ],
        "terminology": {
            "internal_terms": {"病丛率": "发病丛数占调查总丛数的比例"},
            "colloquial_map": {"病丛率": "有多少丛稻子出问题"},
            "display_metrics": ["病丛率"],
        },
        "timing_rules": [{"id": "rice_rain", "when": {"weather": "continuous_rain"}, "prompt": "注意排水"}],
        "output_template": {
            "section_order": ["现状判断", "处置建议"],
            "required_sections": ["现状判断"],
        },
        "calendar_source": "rice.growth_stage",
        "followup_policy": {"triggers": [{"after_days": 7, "ask": "效果如何"}]},
        "offline_questions": [],
        "confusable_items": [],
    }
    base.update(overrides)
    return base


def test_field_count_is_17() -> None:
    """字段数固定为 17：技术方案 9.1 的 16 项 + T3-06 的 `urgency_specs`（紧急双条件）。"""
    assert len(ProcessConfig.model_fields) == 17


def test_valid_config_loads() -> None:
    config = ProcessConfig.model_validate(_minimal())
    assert config.process_id.value == "rice"
    assert config.version == 1
    assert config.panel_enabled is False
    assert config.object_aliases == []
    assert config.banned_topics == []


def test_unknown_field_rejected() -> None:
    """`extra="forbid"`：YAML 里拼错字段名必须立即报错而不是被安静忽略。"""
    with pytest.raises(ValidationError) as exc:
        ProcessConfig.model_validate(_minimal(role_definiton="拼错的字段名"))
    assert "role_definiton" in str(exc.value)


def test_unknown_enum_value_rejected() -> None:
    with pytest.raises(ValidationError):
        ProcessConfig.model_validate(_minimal(process_id="bees"))


def test_slot_group_accepts_chinese_value() -> None:
    config = ProcessConfig.model_validate(_minimal())
    assert config.required_slots[0].group.value == "病虫情"


def test_frozen_config_rejects_assignment() -> None:
    """`frozen=True`：同一次问诊内配置恒定，是结构性保证而非约定。"""
    config = ProcessConfig.model_validate(_minimal())
    with pytest.raises(ValidationError):
        config.version = 2


def test_required_slot_keys_filters_optional() -> None:
    payload = _minimal(
        required_slots=[
            {
                "key": "a",
                "label": "A",
                "display_label": "a",
                "group": "病虫情",
                "owner": "interviewer",
                "required": True,
            },
            {
                "key": "b",
                "label": "B",
                "display_label": "b",
                "group": "肥情",
                "owner": "interviewer",
                "required": False,
            },
        ]
    )
    config = ProcessConfig.model_validate(payload)
    assert config.required_slot_keys() == ["a"]


def test_slot_spec_lookup() -> None:
    config = ProcessConfig.model_validate(_minimal())
    assert config.slot_spec("rice_symptom_part") is not None
    assert config.slot_spec("not_declared") is None


def test_redline_rule_ids_splits_comma() -> None:
    """逗号分隔的 `rule` 字段要展开，供闸门做 `checked_rules` 反查。"""
    payload = _minimal(
        redlines=[{"id": "TEA-03", "desc": "须核算安全间隔期", "rule": "R-INTERVAL,R-MAX-USE", "severity": "block"}]
    )
    config = ProcessConfig.model_validate(payload)
    assert config.redline_rule_ids() == ["R-INTERVAL", "R-MAX-USE"]


def test_redline_rule_ids_strips_whitespace() -> None:
    payload = _minimal(redlines=[{"id": "X-01", "desc": "d", "rule": " R-INTERVAL , R-MAX-USE ", "severity": "warn"}])
    config = ProcessConfig.model_validate(payload)
    assert config.redline_rule_ids() == ["R-INTERVAL", "R-MAX-USE"]


def test_check_owns() -> None:
    config = ProcessConfig.model_validate(_minimal())
    assert config.check_owns("rice_symptom_part", "interviewer") is True
    assert config.check_owns("rice_symptom_part", "field_observer") is False
    assert config.check_owns("not_declared", "interviewer") is False


def test_confusable_pair_requires_features_for_every_candidate() -> None:
    """缺决定性特征会让聚合器在硬判据分支上遇 `KeyError`，必须在配置期拦住。"""
    with pytest.raises(ValidationError) as exc:
        ProcessConfig.model_validate(
            _minimal(
                confusable_items=[
                    {
                        "candidates": ["青枯病", "枯萎病"],
                        "decisive_features": {"青枯病": ["维管束黄褐色"]},
                    }
                ]
            )
        )
    assert "枯萎病" in str(exc.value)


def test_confusable_pair_valid() -> None:
    config = ProcessConfig.model_validate(
        _minimal(
            panel_enabled=True,
            confusable_items=[
                {
                    "candidates": ["青枯病", "枯萎病"],
                    "decisive_features": {
                        "青枯病": ["叶片保绿而急速萎蔫"],
                        "枯萎病": ["无菌脓"],
                    },
                }
            ],
        )
    )
    assert config.confusable_items[0].must_exclude is True


def test_output_template_defaults() -> None:
    config = ProcessConfig.model_validate(_minimal(output_template={"section_order": ["现状判断"]}))
    assert config.output_template.max_chars == 400
    assert config.output_template.required_sections == []


def test_offline_question_default_answer_type() -> None:
    config = ProcessConfig.model_validate(_minimal(offline_questions=[{"slot_key": "k", "text": "有没有遮阴"}]))
    assert config.offline_questions[0].answer_type.value == "single"


def test_redline_severity_is_closed_enum() -> None:
    """`severity` 只有 block 与 warn 两个取值，写别的必须报错。"""
    with pytest.raises(ValidationError):
        ProcessConfig.model_validate(
            _minimal(redlines=[{"id": "X-01", "desc": "d", "rule": "R-REGION", "severity": "degrade"}])
        )
