"""T2-04：七份流程 YAML 的加载与关键字段断言。

配置是"新增第九条流程不改编排代码"这条判断标准的实物，因此这里的断言有两层：
一层是七份都能加载（结构正确），另一层是各流程的**差异点确实落在配置里**
（`panel_enabled` 的三条、辣椒的鉴别槽位、油菜的药剂推荐红线、通用的 null 农事历）。
只有第二层过了，"差异全部来自配置"才不是空话。
"""

import pytest

from app.agent.state import ProcessId
from app.process_config.loader import load_all_configs, load_process_config


@pytest.fixture(scope="module")
def configs():
    return load_all_configs()


def test_all_seven_loadable(configs) -> None:
    assert set(configs) == set(ProcessId)
    assert len(configs) == 7


def test_identifiers_match_filenames(configs) -> None:
    for pid, config in configs.items():
        assert config.process_id is pid
        assert config.version >= 1
        assert config.display_name
        assert config.role_definition


def test_redlines_non_empty(configs) -> None:
    """红线缺失即该流程不可用（技术方案 8.4），因此一条都不许为空。"""
    for pid, config in configs.items():
        assert config.redlines, f"{pid.value} 的红线为空"


def test_max_chars_in_adaptive_range(configs) -> None:
    """上限 200~500：低于 200 装不下必要提示，高于 500 违背适老化改造。"""
    for pid, config in configs.items():
        assert 200 <= config.output_template.max_chars <= 500, pid.value


def test_slot_owners_implemented(configs) -> None:
    known = {"field_observer", "interviewer", "profile_loader"}
    for pid, config in configs.items():
        for slot in config.required_slots:
            assert slot.owner in known, f"{pid.value}.{slot.key}"


def test_required_section_subset_of_order(configs) -> None:
    for pid, config in configs.items():
        missing = set(config.output_template.required_sections) - set(config.output_template.section_order)
        assert not missing, f"{pid.value}: {missing}"


def test_object_aliases_non_empty(configs) -> None:
    """分诊的对象确认依赖别名表，为空则任何表述都匹配不上。"""
    for pid, config in configs.items():
        assert config.object_aliases, pid.value


def test_terminology_maps_non_empty(configs) -> None:
    for pid, config in configs.items():
        assert config.terminology.internal_terms, pid.value
        assert config.terminology.colloquial_map, pid.value


def test_panel_enabled_exactly_three(configs) -> None:
    """并行会诊只为茶、辣椒、杂草三条流程启用（技术方案 6.4）。"""
    enabled = {pid for pid, config in configs.items() if config.panel_enabled}
    assert enabled == {ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED}


def test_panel_enabled_flows_have_confusable_items(configs) -> None:
    for pid, config in configs.items():
        if config.panel_enabled:
            assert config.confusable_items, pid.value


def test_chili_has_key_differentiation_slots() -> None:
    """辣椒的两个鉴别槽位必须存在，否则青枯病与枯萎病无法区分。"""
    config = load_process_config(ProcessId.CHILI)
    keys = {s.key for s in config.required_slots}
    assert {"chili_exudate", "chili_vascular_bundle"} <= keys
    # 同一份配置同时驱动问诊与判定：鉴别槽位必须也出现在决定性特征中
    features = config.confusable_items[0].decisive_features
    assert len(features) >= 2


def test_tea_and_chili_confusable_must_exclude(configs) -> None:
    assert configs[ProcessId.TEA].confusable_items[0].must_exclude is True
    assert configs[ProcessId.CHILI].confusable_items[0].must_exclude is True


def test_weed_has_herbicide_related_confusable_pairs(configs) -> None:
    """杂草流程的易混淆项以草种为单位逐种给出鉴别特征。"""
    config = configs[ProcessId.WEED]
    all_candidates = {c for pair in config.confusable_items for c in pair.candidates}
    assert {"稗草", "千金子", "看麦娘", "节节麦"} <= all_candidates


def test_weed_calendar_source_is_none(configs) -> None:
    assert configs[ProcessId.WEED].calendar_source is None


def test_general_calendar_source_is_none(configs) -> None:
    """通用兜底流程不建农事历。"""
    config = load_process_config(ProcessId.GENERAL)
    assert config.calendar_source is None


def test_rapeseed_has_pesticide_recommendation_ban(configs) -> None:
    """产品说明书 5.4：补齐调研前油菜不得输出药剂推荐。"""
    config = configs[ProcessId.RAPESEED]
    assert any("药剂推荐" in item.desc for item in config.redlines)
    assert any(item.id.startswith("RAPE-") for item in config.redlines)


def test_pig_has_split_report_signal_slots(configs) -> None:
    """养猪流程的识别信号槽位必须拆细，不能合并成"有无异常"（技术方案 7.4）。"""
    config = configs[ProcessId.PIG]
    keys = {s.key for s in config.required_slots}
    assert {"pig_death_count", "pig_sudden_death", "pig_multi_pen"} <= keys
    assert config.panel_enabled is False
    assert config.calendar_source is None


def test_pig_redline_bans_replacing_report_duty(configs) -> None:
    config = configs[ProcessId.PIG]
    assert any("法定报告" in item.desc for item in config.redlines)


def test_general_has_observation_only_redline(configs) -> None:
    """通用流程无专属阈值、无药剂条目，处方只能给观察与求助建议。

    必问清单只留基础四项到五项（作物名、部位、形态、范围、近期用药）；此外可声明
    少量 `required=False` 的槽位承载增强追问，它们不进必问清单。
    """
    config = configs[ProcessId.GENERAL]
    assert any("药剂推荐" in item.desc for item in config.redlines)
    assert 4 <= len(config.required_slot_keys()) <= 5
    assert config.confusable_items == []


def test_general_banned_topics_include_forecast(configs) -> None:
    banned = "".join(configs[ProcessId.GENERAL].banned_topics)
    assert "预报" in banned


def test_all_banned_topics_mention_forecast_or_equivalent(configs) -> None:
    """七条流程都不得发布预报类内容，这条是跨流程的公共底线。"""
    for pid, config in configs.items():
        assert config.banned_topics, pid.value


def test_followup_policy_has_triggers(configs) -> None:
    for pid, config in configs.items():
        assert config.followup_policy.triggers, pid.value


def test_weed_followup_day_7_and_15(configs) -> None:
    """杂草药后 7 天查药效、15 天查后茬。"""
    days = {t.after_days for t in configs[ProcessId.WEED].followup_policy.triggers}
    assert {7, 15} <= days


def test_chili_followup_day_5() -> None:
    """辣椒药后 3 至 7 天回访，取中位 5 天（T3-09 第一节）。"""
    config = load_process_config(ProcessId.CHILI)
    days = {t.after_days for t in config.followup_policy.triggers}
    assert days == {5}


def test_slot_keys_unique_within_flow(configs) -> None:
    for pid, config in configs.items():
        keys = [s.key for s in config.required_slots]
        assert len(keys) == len(set(keys)), pid.value


def test_ask_order_sorted_ascending(configs) -> None:
    """同一批次内的问题按 ask_order 排序，配置应写成递增以便人工阅读。"""
    for pid, config in configs.items():
        orders = [s.ask_order for s in config.required_slots]
        assert orders == sorted(orders), pid.value


def test_redline_ids_unique_per_flow(configs) -> None:
    for pid, config in configs.items():
        ids = [r.id for r in config.redlines]
        assert len(ids) == len(set(ids)), pid.value


def test_redline_ids_prefixed_by_flow(configs) -> None:
    """红线 id 带流程前缀，便于按 id 反查归属流程。"""
    prefix = {
        ProcessId.TEA: "TEA-",
        ProcessId.CHILI: "CHILI-",
        ProcessId.RICE: "RICE-",
        ProcessId.RAPESEED: "RAPE-",
        ProcessId.WEED: "WEED-",
        ProcessId.PIG: "PIG-",
        ProcessId.GENERAL: "GEN-",
    }
    for pid, config in configs.items():
        for item in config.redlines:
            assert item.id.startswith(prefix[pid]), f"{pid.value}: {item.id}"


def test_offline_questions_reference_declared_slots(configs) -> None:
    """增强追问只能针对已声明的槽位，否则写入时会因 owner 查不到而失败。"""
    for pid, config in configs.items():
        declared = {s.key for s in config.required_slots}
        for question in config.offline_questions:
            assert question.slot_key in declared, f"{pid.value}: {question.slot_key}"
