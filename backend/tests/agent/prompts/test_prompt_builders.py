"""T2-60：提示词模板组织、结构化拼装与版本管理。

用例按四组组织：

1. 七步拼装：问诊提示词的七个部分各自的内容都在，尤其是随流程变化的三段（术语、红线、
   鉴别点）；输出契约注入 `InterviewBatch` 的 JSON Schema；候选问题池是封闭集合；
2. 各智能体的模板差异：判定与处方无候选问题池，处方含两条硬规则，分诊含"不得生成任何
   问题"；术语两套分开（`format_colloquial` 含"说成"而 `format_terms` 不含）；
3. 候选注入与流程隔离：候选与 `select_candidate_slots` 的输出一致且不超过 `top_k`，候选
   为空时给出"应进入下一环节"提示，茶与稻的 `display_metrics` 互不串台；
4. 版本与边界：`PROMPT_VERSIONS` 覆盖全部模板名且 `current_prompt_version` 返回整数、
   提示词不含任何药剂名、构造过程零 HTTP 请求、`formatters` 全是纯函数。

这些用例只喂流程配置与状态字典，不连库、不连模型、不连网络：提示词构造是纯函数，因此
可以直接断言生成的文本含哪些片段。
"""

import copy
import inspect
import json
from typing import Any

import pytest
import respx

from app.agent.agents.candidate_pool import CandidateSlot, select_candidate_slots
from app.agent.contracts import InterviewBatch
from app.agent.prompts import (
    PROMPT_VERSIONS,
    TOP_K,
    build_diagnoser_prompt,
    build_interviewer_prompt,
    build_prescriber_prompt,
    build_triage_prompt,
    current_prompt_version,
    format_colloquial,
    format_confusables,
    format_terms,
)
from app.agent.prompts.formatters import format_candidates, format_redlines
from app.agent.state import ConsultationState, ProcessId, SlotValue
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.models import ProcessConfig

_TEA = ProcessId.TEA
_RICE = ProcessId.RICE


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(_TEA)


@pytest.fixture(scope="module")
def rice_config() -> ProcessConfig:
    return get_process_config(_RICE)


@pytest.fixture(scope="module")
def configs() -> dict[ProcessId, ProcessConfig]:
    return load_all_configs()


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": "我家茶树叶子发黄"}],
    }
    state.update(overrides)
    return state


def _slot(key: str, status: str, value: str | None = None, source: str = "user_answer") -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source=source)  # type: ignore[arg-type]


def _system(messages: list[dict[str, Any]]) -> str:
    return messages[0]["content"]


def _user(messages: list[dict[str, Any]]) -> str:
    return messages[1]["content"]


# ---- 第一组：七步拼装 ----


def test_interviewer_prompt_has_system_and_user_only(tea_config: ProcessConfig) -> None:
    """输出固定为 system + user 两条：网关把 schema 传适配器是另一层，这里只管文本。"""
    messages = build_interviewer_prompt(tea_config, _state(), candidates=[])

    assert [m["role"] for m in messages] == ["system", "user"]


def test_interviewer_prompt_contains_seven_sections(tea_config: ProcessConfig) -> None:
    """七个部分各自的内容都在：角色、契约、硬规则、术语两套、红线、鉴别点、候选池。"""
    messages = build_interviewer_prompt(tea_config, _state())
    system = _system(messages)
    user = _user(messages)

    # 1. 角色定位（按流程）
    assert tea_config.role_definition in system
    # 2. 输出契约（共用，强制 JSON）
    assert json.dumps(InterviewBatch.model_json_schema(), ensure_ascii=False) in system
    # 3. 硬规则（共用）
    assert "每轮最多问 2 个问题" in system
    assert "不得询问已经知道答案的信息" in system
    assert "换个说法再问一遍" in system
    # 4. 术语规范（按流程，两套都注入）
    for metric in tea_config.terminology.display_metrics:
        assert metric in system
    for spoken in tea_config.terminology.colloquial_map.values():
        assert spoken in system
    # 5. 禁用输出（按流程红线）
    for redline in tea_config.redlines:
        assert redline.id in system
    # 6. 易混淆项（按流程）
    for pair in tea_config.confusable_items:
        for candidate in pair.candidates:
            assert candidate in system
    # 7. 候选问题池（代码筛选，进 user）
    assert "候选追问方向" in user
    assert "不得超出" in user


def test_redlines_appear_in_banned_section(tea_config: ProcessConfig) -> None:
    """红线文本紧跟"严禁输出"之后，出现在禁用输出部分。"""
    system = _system(build_interviewer_prompt(tea_config, _state()))

    assert f"严禁输出：{format_redlines(tea_config.redlines)}" in system
    assert f"{tea_config.redlines[0].id} {tea_config.redlines[0].desc}" in system


def test_confusables_appear_in_discrimination_section(tea_config: ProcessConfig) -> None:
    """易混淆项的候选名与其决定性特征一起出现在鉴别点部分。"""
    system = _system(build_interviewer_prompt(tea_config, _state()))
    pair = tea_config.confusable_items[0]

    assert "你必须主动追问下列鉴别点" in system
    for candidate in pair.candidates:
        assert candidate in system
    for features in pair.decisive_features.values():
        for feature in features:
            assert feature in system


def test_interviewer_injects_region_code(tea_config: ProcessConfig) -> None:
    """地区编码由调用方注入，进 user：它是"档案地区"而非流程差异，故不放 system。"""
    user = _user(build_interviewer_prompt(tea_config, _state(), region_code="330000"))

    assert "330000" in user


# ---- 第二组：各智能体的模板差异 ----


def test_diagnoser_prompt_has_no_candidate_pool(tea_config: ProcessConfig) -> None:
    """判定不生成问题，故不含候选问题池：出现候选清单会让模型误以为要追问。"""
    messages = build_diagnoser_prompt(tea_config, _state(), [])
    text = _system(messages) + _user(messages)

    assert "候选追问方向" not in text
    assert "不得生成任何问题" not in text


def test_diagnoser_prompt_forbids_percentages(tea_config: ProcessConfig) -> None:
    """判定提示词显式禁止百分比与分数，并注入鉴别点全文。"""
    system = _system(build_diagnoser_prompt(tea_config, _state(), []))

    assert "不得输出百分比或分数" in system
    assert "你必须主动追问下列鉴别点" not in system
    assert "下列候选必须显式鉴别" in system


def test_prescriber_prompt_has_hard_rules(tea_config: ProcessConfig) -> None:
    """处方提示词含两条硬规则，且不含候选问题池。"""
    messages = build_prescriber_prompt(tea_config, _state(), [])
    system = _system(messages)
    text = system + _user(messages)

    assert "不得新增、修改或删除任何药剂条目" in system
    assert "不得改动任何数字" in system
    assert "候选追问方向" not in text


def test_prescriber_prompt_injects_output_template(tea_config: ProcessConfig) -> None:
    """输出模板的段落顺序、字数上限与必含小节随流程注入。"""
    system = _system(build_prescriber_prompt(tea_config, _state(), []))
    template = tea_config.output_template

    for section in template.section_order:
        assert section in system
    assert str(template.max_chars) in system
    for section in template.required_sections:
        assert section in system


def test_triage_prompt_forbids_questions(configs: dict[ProcessId, ProcessConfig]) -> None:
    """分诊提示词含"不得生成任何问题"，与契约层不含问题字段形成双保险。"""
    messages = build_triage_prompt(configs, _state(), current_message="我家茶树叶子发黄")
    system = _system(messages)

    assert "不得生成任何问题" in system


def test_triage_prompt_lists_all_seven_processes(configs: dict[ProcessId, ProcessConfig]) -> None:
    """分诊列出七条流程及其识别词：流程清单来自配置而不是硬编码表。"""
    system = _system(build_triage_prompt(configs, _state(), current_message="茶树叶子发黄"))

    for pid in ProcessId:
        config = configs.get(pid)
        if config is None:
            continue
        assert pid.value in system
        assert config.display_name in system


def test_triage_prompt_uses_single_process_terminology_absent(configs: dict[ProcessId, ProcessConfig]) -> None:
    """分诊阶段流程未定，故不注入单个流程的术语与红线。"""
    tea = configs[_TEA]
    system = _system(build_triage_prompt(configs, _state(), current_message="茶树叶子发黄"))

    assert f"严禁输出：{format_redlines(tea.redlines)}" not in system
    assert "使用以下术语" not in system


def test_colloquial_and_terms_differ(tea_config: ProcessConfig) -> None:
    """两套术语分开：口语化对照含"说成"，内部口径不含。"""
    terms = format_terms(tea_config.terminology.display_metrics)
    colloquial = format_colloquial(tea_config.terminology.colloquial_map)

    assert "说成" in colloquial
    assert "说成" not in terms
    assert terms != colloquial
    for metric in tea_config.terminology.display_metrics:
        assert metric in terms


# ---- 第三组：候选注入与流程隔离 ----


def test_candidates_match_selector_and_within_top_k(tea_config: ProcessConfig) -> None:
    """候选清单与 `select_candidate_slots` 的输出一致，且条数不超过 `top_k`。"""
    state = _state()
    pool = select_candidate_slots(tea_config, state, top_k=TOP_K)

    user = _user(build_interviewer_prompt(tea_config, state, candidates=pool.candidates))

    assert len(pool.candidates) <= TOP_K
    for candidate in pool.candidates:
        assert candidate.display_label in user
        assert f"槽位 {candidate.slot_key}" in user


def test_custom_top_k_caps_candidates(tea_config: ProcessConfig) -> None:
    """`top_k` 透传到筛选器：传入 2 时候选只剩两条。"""
    pool = select_candidate_slots(tea_config, _state(), top_k=2)
    user = _user(build_interviewer_prompt(tea_config, _state(), candidates=pool.candidates))

    assert len(pool.candidates) <= 2
    for candidate in pool.candidates:
        assert f"槽位 {candidate.slot_key}" in user


def test_empty_candidates_prompts_next_stage(tea_config: ProcessConfig) -> None:
    """候选为空时给出"应进入下一环节"提示：不让模型自由发挥生成流程外的问题。"""
    user = _user(build_interviewer_prompt(tea_config, _state(), candidates=[]))

    assert "（本轮无候选问题，应进入下一环节）" in user


def test_tea_and_rice_display_metrics_are_isolated(tea_config: ProcessConfig, rice_config: ProcessConfig) -> None:
    """提示词只含当前流程的 `display_metrics`，不含其他流程的。"""
    tea_text = "".join(m["content"] for m in build_interviewer_prompt(tea_config, _state()))
    rice_text = "".join(m["content"] for m in build_interviewer_prompt(rice_config, _state()))

    assert "叶发病率" in tea_text
    assert "病丛率" not in tea_text

    assert "病丛率" in rice_text
    assert "叶发病率" not in rice_text


def test_no_pesticide_name_is_injected(tea_config: ProcessConfig) -> None:
    """提示词不含任何知识库药剂名：事实由检索注入候选区，不由提示词模板携带。"""
    text = "".join(m["content"] for m in build_interviewer_prompt(tea_config, _state()))

    assert "示例药剂A" not in text
    assert "kb_chili_pest_001" not in text
    assert "ml/亩" not in text


# ---- 第四组：版本与边界 ----


def test_prompt_versions_cover_all_names() -> None:
    """`PROMPT_VERSIONS` 覆盖全部模板名，每个都是正整数。"""
    assert set(PROMPT_VERSIONS) == {
        "triage",
        "interviewer",
        "retriever",
        "diagnoser",
        "prescriber",
        "report_guard",
        "snapshot_writer",
    }
    assert all(isinstance(version, int) and version >= 1 for version in PROMPT_VERSIONS.values())


def test_current_prompt_version_returns_int() -> None:
    """`current_prompt_version` 返回整数；未登记的模板名抛 `KeyError` 而不是给默认值。"""
    version = current_prompt_version("interviewer")

    assert isinstance(version, int)
    assert version == PROMPT_VERSIONS["interviewer"]

    with pytest.raises(KeyError):
        current_prompt_version("not-a-template")


def test_prompts_are_deterministic(tea_config: ProcessConfig) -> None:
    """同一配置与状态两次构造返回完全相同的文本（可重放的前提）。"""
    state = _state()
    first = build_interviewer_prompt(tea_config, state)
    second = build_interviewer_prompt(tea_config, state)

    assert first == second


def test_builders_make_no_network_request(
    tea_config: ProcessConfig, configs: dict[ProcessId, ProcessConfig], respx_mock: respx.Router
) -> None:
    """四个构造入口全程不得发起 HTTP 请求：提示词构造是纯函数。"""
    build_interviewer_prompt(tea_config, _state())
    build_diagnoser_prompt(tea_config, _state(), [])
    build_prescriber_prompt(tea_config, _state(), [])
    build_triage_prompt(configs, _state(), current_message="茶树叶子发黄")

    assert respx_mock.calls == []


def test_formatters_are_pure(tea_config: ProcessConfig) -> None:
    """纯函数：相同输入返回相同输出，且不修改入参对象。"""
    metrics = list(tea_config.terminology.display_metrics)
    pairs = list(tea_config.confusable_items)
    redlines = list(tea_config.redlines)
    colloquial_map = dict(tea_config.terminology.colloquial_map)
    candidates = [CandidateSlot(slot_key="k", display_label="标签", group="管理", required=True, options=["a"])]

    before_terminology = copy.deepcopy(tea_config.terminology)
    before_pairs = copy.deepcopy(tea_config.confusable_items)

    assert format_terms(metrics) == format_terms(metrics)
    assert metrics == list(tea_config.terminology.display_metrics)

    assert format_colloquial(colloquial_map) == format_colloquial(colloquial_map)
    assert colloquial_map == dict(tea_config.terminology.colloquial_map)

    assert format_redlines(redlines) == format_redlines(redlines)
    assert len(redlines) == len(tea_config.redlines)

    assert format_confusables(pairs) == format_confusables(pairs)
    assert copy.deepcopy(tea_config.confusable_items) == before_pairs
    assert tea_config.terminology == before_terminology

    assert format_candidates(candidates) == format_candidates(candidates)
    assert candidates[0].slot_key == "k"


def test_builders_do_not_mutate_state(tea_config: ProcessConfig) -> None:
    """构造过程只读 state：已填槽位不会被写回或改动。"""
    slots = {"tea_symptom_part": _slot("tea_symptom_part", "filled", "叶片")}
    state = _state(slots=slots)

    build_interviewer_prompt(tea_config, state)
    build_diagnoser_prompt(tea_config, state, [])
    build_prescriber_prompt(tea_config, state, [])

    assert state["slots"] is slots
    assert slots["tea_symptom_part"].value == "叶片"


def test_formatters_have_no_gateway_or_session_dependency() -> None:
    """构造入口签名里不得出现网关或数据库会话：提示词构造不依赖模型与数据库。"""
    for builder in (build_diagnoser_prompt, build_interviewer_prompt, build_prescriber_prompt, build_triage_prompt):
        params = inspect.signature(builder).parameters

        assert not {"gateway", "session", "db", "engine"} & set(params)
