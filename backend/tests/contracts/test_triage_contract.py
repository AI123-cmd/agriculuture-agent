"""T2-09：分诊契约的正反例与对象确认的可重放性。

反例比正例重要：`fallback_reason` 与 `handoff_to_report` 两条校验器拦的都是"看起来能
跑、但语义已经错了"的输出，不会以异常形式暴露，只会在下游表现为分流错误或跳过现场采集。
"""

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    ObservationResult,
    ObjectConfirmRequest,
    TriageResult,
    match_object_alias,
    resolve_process_hint,
)
from app.agent.state import ProcessId
from app.process_config import ProcessConfig
from app.process_config.loader import load_all_configs
from app.process_config.models import ProcessConfig as ProcessConfigModel


@pytest.fixture(scope="module")
def configs() -> dict[ProcessId, ProcessConfig]:
    return load_all_configs()


def _triage(**overrides) -> dict:
    base = {
        "process_id": ProcessId.RICE,
        "object_confirmed": True,
        "object_label": "水稻·3亩",
        "slot_gaps": ["rice_symptom_part"],
        "profile_ready": True,
    }
    base.update(overrides)
    return base


def test_valid_result_passes() -> None:
    result = TriageResult(**_triage())
    assert result.process_id is ProcessId.RICE
    assert result.handoff_to_report is False
    assert result.fallback_reason is None


def test_general_without_fallback_reason_raises() -> None:
    with pytest.raises(ValidationError, match="fallback_reason"):
        TriageResult(**_triage(process_id=ProcessId.GENERAL))


def test_general_with_fallback_reason_passes() -> None:
    result = TriageResult(**_triage(process_id=ProcessId.GENERAL, fallback_reason="未匹配到任何流程别名"))
    assert result.process_id is ProcessId.GENERAL


def test_handoff_to_report_only_for_pig() -> None:
    with pytest.raises(ValidationError, match="handoff_to_report"):
        TriageResult(**_triage(process_id=ProcessId.WEED, handoff_to_report=True))


def test_pig_handoff_to_report_passes() -> None:
    result = TriageResult(**_triage(process_id=ProcessId.PIG, handoff_to_report=True))
    assert result.handoff_to_report is True


def test_object_label_is_required() -> None:
    payload = _triage()
    payload.pop("object_label")
    with pytest.raises(ValidationError):
        TriageResult(**payload)


def test_extra_field_rejected() -> None:
    """分诊不得生成问题，因此契约里没有承载问题文本的字段；多传的键一律报错。"""
    with pytest.raises(ValidationError):
        TriageResult(**_triage(questions=["叶片怎么了"]))


def test_model_fields_match_tech_spec() -> None:
    assert set(TriageResult.model_fields) == {
        "process_id",
        "object_confirmed",
        "object_label",
        "slot_gaps",
        "profile_ready",
        "fallback_reason",
        "handoff_to_report",
    }


def test_json_schema_generates() -> None:
    """结构化输出约束要用它，生成失败会让模型路由直接不可用。"""
    schema = TriageResult.model_json_schema()
    assert "process_id" in schema["properties"]


def test_json_schema_generates_for_observation() -> None:
    assert "env_source" in ObservationResult.model_json_schema()["properties"]


def test_object_confirm_request_roundtrip() -> None:
    req = ObjectConfirmRequest(
        asked_process_id=ProcessId.TEA,
        profile_process_id=ProcessId.RICE,
        candidate_labels=["茶树", "水稻"],
        question_text="您档案里登记的是水稻，这次问的是茶吗？",
        matched_alias="茶",
    )
    assert req.profile_process_id is ProcessId.RICE
    assert req.matched_alias == "茶"


def test_match_object_alias_hits_tea(configs) -> None:
    assert match_object_alias("我家茶树叶子发黄", configs) is ProcessId.TEA


def test_match_object_alias_hits_weed(configs) -> None:
    """杂草是"跨作物问题类型"，别名必须覆盖农户的日常说法（"地里有草"）。"""
    assert match_object_alias("地里有草", configs) is ProcessId.WEED


def test_match_object_alias_returns_none_when_no_hit(configs) -> None:
    assert match_object_alias("今天天气不错", configs) is None


def test_match_object_alias_empty_text(configs) -> None:
    assert match_object_alias("", configs) is None


def test_match_object_alias_longest_wins(configs) -> None:
    """油菜花与油菜都能命中油菜流程，取最长者；断言的是结果流程而非匹配长度，
    因为别名包含关系在配置里真实存在。"""
    assert match_object_alias("油菜花开得挺好", configs) is ProcessId.RAPESEED


def test_match_object_alias_is_deterministic(configs) -> None:
    """路由必须可确定性重放：同一输入一百次调用结果完全一致。

    这条断言针对的是"命中长度相同"的情形：若裁决依赖 `dict` 迭代顺序或集合无序性，
    结果会随进程（甚至随哈希种子）变化，线上表现为"同样的问法有时分流到不同流程"。
    """
    results = {match_object_alias("我家茶树叶子发黄", configs) for _ in range(100)}
    assert results == {ProcessId.TEA}


def test_match_object_alias_tie_break_uses_declaration_order() -> None:
    """长度相同时取 `ProcessId` 声明序靠前者，据此保证结果稳定且可评审。"""

    def _config(pid: ProcessId, aliases: list[str]) -> ProcessConfigModel:
        return ProcessConfigModel(
            process_id=pid,
            version=1,
            display_name="x",
            role_definition="x",
            required_slots=[],
            redlines=[],
            terminology={"internal_terms": {}, "colloquial_map": {}, "display_metrics": []},
            timing_rules=[],
            output_template={"section_order": [], "required_sections": []},
            calendar_source=None,
            followup_policy={"triggers": []},
            offline_questions=[],
            confusable_items=[],
            object_aliases=aliases,
        )

    tie = {
        ProcessId.CHILI: _config(ProcessId.CHILI, ["同长词"]),
        ProcessId.TEA: _config(ProcessId.TEA, ["同长词"]),
    }
    # 传入顺序故意把 CHILI 放前：结果必须由声明序决定，而不是由字典插入序决定
    assert match_object_alias("这里有同长词出现", tie) is ProcessId.TEA


def test_contract_dump_and_load_roundtrip() -> None:
    """契约进出 `ConsultationState` 的固定通道：`mode="json"` 序列化 + 校验回读。"""
    from app.agent.contracts import dump_contract, load_contract

    payload = dump_contract(TriageResult(**_triage()))
    assert isinstance(payload["process_id"], str) and payload["process_id"] == "rice"
    restored = load_contract(TriageResult, payload)
    assert restored.process_id is ProcessId.RICE
    assert restored == TriageResult(**_triage())


# ---- 入口提示归一（resolve_process_hint） ----


def test_resolve_process_hint_accepts_english_id(configs) -> None:
    """英文流程标识走精确匹配（直达流程按钮用的是它）。"""
    assert resolve_process_hint("weed", configs) is ProcessId.WEED
    assert resolve_process_hint("pig", configs) is ProcessId.PIG


def test_resolve_process_hint_accepts_crop_name(configs) -> None:
    """农户可见的作物名经 `object_aliases` 归一（小程序「作物模型」按钮用的是它）。

    这条是本次修复的核心：此前直接 `ProcessId("水稻")` 会抛 `ValueError`，使接口 500。
    """
    assert resolve_process_hint("水稻", configs) is ProcessId.RICE
    assert resolve_process_hint("辣椒", configs) is ProcessId.CHILI
    assert resolve_process_hint("油菜", configs) is ProcessId.RAPESEED
    assert resolve_process_hint("茶叶", configs) is ProcessId.TEA


def test_resolve_process_hint_longest_alias_wins(configs) -> None:
    """别名包含关系下取最长者，与 `match_object_alias` 同口径。"""
    assert resolve_process_hint("油菜花", configs) is ProcessId.RAPESEED


def test_resolve_process_hint_unmatched_returns_none(configs) -> None:
    """「通用农业」及未登记作物不属于任何专属流程，返回 `None` 由调用方兜底。"""
    assert resolve_process_hint("通用农业", configs) is None
    assert resolve_process_hint("番茄", configs) is None


def test_resolve_process_hint_blank_returns_none(configs) -> None:
    """空串、纯空白、`None` 一律返回 `None`，不参与匹配。"""
    assert resolve_process_hint(None, configs) is None
    assert resolve_process_hint("", configs) is None
    assert resolve_process_hint("   ", configs) is None
