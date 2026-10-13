"""T2-17：追问候选池的三步筛选。

用例按四组组织：

1. 第一步剔除：已确认与"农户答不出"都不再问（后者是「不反复逼问」的直接实现）；
2. 第二步剔除：可由档案/环境直接读的项，以及追问超限的项；
3. 第三步排序：必问优先、易混淆项相关优先、同分按 `ask_order`、结果稳定可重放；
4. 边界：`top_k` 上限、无模型无数据库依赖。

筛选器是纯函数：全部用例只喂流程配置与状态字典，不连库、不连模型。
"""

import inspect

import pytest

from app.agent.agents.candidate_pool import CandidatePool, select_candidate_slots
from app.agent.contracts import ImageCandidate, ObservationResult
from app.agent.state import ConsultationState, ProcessId, SlotGroup, SlotValue
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig, SlotSpec

_TEA = ProcessId.TEA


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(_TEA)


@pytest.fixture(scope="module")
def chili_config() -> ProcessConfig:
    return get_process_config(ProcessId.CHILI)


def _state(**overrides: object) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": "我家茶树叶子发黄"}],
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def _slot(key: str, status: str, value: str | None = None, source: str = "user_answer") -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source=source)  # type: ignore[arg-type]


# ---- 第一步：剔除已 filled / unknown ----


def test_filled_slot_is_excluded(tea_config: ProcessConfig) -> None:
    """已确认的槽位不再出现在候选里，并记录剔除原因。"""
    slots = {"tea_symptom_part": _slot("tea_symptom_part", "filled", "叶片")}

    pool = select_candidate_slots(tea_config, _state(slots=slots))

    assert "tea_symptom_part" not in pool.keys
    assert pool.excluded["tea_symptom_part"] == "已确认"


def test_unknown_slot_is_excluded_with_reason(tea_config: ProcessConfig) -> None:
    """`unknown` 是合法答案：不再问，且 `excluded` 记录"农户已表示不清楚"。"""
    slots = {"tea_symptom_part": _slot("tea_symptom_part", "unknown")}

    pool = select_candidate_slots(tea_config, _state(slots=slots))

    assert "tea_symptom_part" not in pool.keys
    assert pool.excluded["tea_symptom_part"] == "农户已表示不清楚"


def test_missing_slot_stays_in_pool(tea_config: ProcessConfig) -> None:
    """`missing`（登记过但没问过）仍要问，与 `unknown` 严格区分。"""
    slots = {"tea_symptom_part": _slot("tea_symptom_part", "missing")}

    pool = select_candidate_slots(tea_config, _state(slots=slots))

    assert "tea_symptom_part" in pool.keys


# ---- 第二步：剔除可自动读取项与超限项 ----


def test_profile_backed_slot_excluded_only_when_value_present(tea_config: ProcessConfig) -> None:
    """档案里确有该值时剔除；只配了 `source_hint: [profile]` 而档案没值时照问。"""
    spec = tea_config.slot_spec("tea_last_pesticide")
    assert spec is not None and "profile" in spec.source_hint, "样例流程应有一个档案来源的槽位"

    without_value = select_candidate_slots(tea_config, _state(profile={}))
    with_value = select_candidate_slots(
        tea_config,
        _state(profile={"slot_values": {"tea_last_pesticide": "20 天前打过一次"}}),
    )

    assert "tea_last_pesticide" in without_value.keys
    assert "tea_last_pesticide" not in with_value.keys
    assert with_value.excluded["tea_last_pesticide"] == "档案或环境已有"


def test_snapshot_growth_stage_excludes_rice_stage_slot() -> None:
    """P0-B 端到端：`build_profile_snapshot` 给的阶段值使水稻生育期槽位不再进候选池。

    这是"档案 → 问诊图"接通后的真实效果：`slot_values` 的键与流程 `required_slots` 一致，
    筛选器据此剔除，农户不会再被追问已经登记过的生育期。
    """
    rice = get_process_config(ProcessId.RICE)
    state = _state(profile={"slot_values": {"rice_growth_stage": "分蘖期"}})

    pool = select_candidate_slots(rice, state, top_k=20)

    assert "rice_growth_stage" not in pool.keys
    assert pool.excluded["rice_growth_stage"] == "档案或环境已有"
    # 同流程另一个档案来源槽位（最近用药）没值，仍要问
    assert "rice_last_pesticide" in pool.keys


def _sensor_slot_config(tea_config: ProcessConfig) -> ProcessConfig:
    """把 `soil_moisture` 作为一个传感器来源槽位加进配置。

    筛选器判定"环境已有"的方式是拿槽位 key 与可用读数/观察事实的标识比对，因此这里
    的槽位 key 必须与读数口径同名（`soil_moisture`），这与 `chili_soil_reading` 这类
    给农户看的槽位名不同——后者要靠观察事实的键来对应。
    """
    sensor_spec = SlotSpec(
        key="soil_moisture",
        label="土壤墒情",
        display_label="地里的湿度",
        group=SlotGroup.SOIL_MOISTURE,
        required=False,
        owner="field_observer",
        source_hint=["sensor"],
        ask_order=90,
    )
    return tea_config.model_copy(update={"required_slots": [*tea_config.required_slots, sensor_spec]})


def test_available_sensor_reading_excludes_matching_slot(tea_config: ProcessConfig) -> None:
    """环境读数可用时，对应的传感器槽位不必再问。"""
    observations = ObservationResult.model_validate(
        {
            "readings": [{"metric": "soil_moisture", "value": 32.0, "unit": "%", "source": "sensor", "quality": "ok"}],
            "image_candidates": [],
            "data_available": True,
            "env_source": "sensor",
        }
    )

    pool = select_candidate_slots(_sensor_slot_config(tea_config), _state(), observations=observations, top_k=20)

    assert "soil_moisture" not in pool.keys
    assert pool.excluded["soil_moisture"] == "档案或环境已有"


def test_unusable_reading_does_not_exclude_slot(tea_config: ProcessConfig) -> None:
    """读数不可用时不算"已有"：传感器槽位保留在候选池里。"""
    observations = ObservationResult.model_validate(
        {
            "readings": [
                {"metric": "soil_moisture", "value": None, "unit": "%", "source": "sensor", "quality": "missing"}
            ],
            "image_candidates": [],
            "data_available": False,
            "unavailable_reason": "传感器离线",
            "env_source": "none",
            "note": "暂时拿不到环境数据，下面的判断主要根据您描述的情况。",
        }
    )

    pool = select_candidate_slots(_sensor_slot_config(tea_config), _state(), observations=observations, top_k=20)

    assert "soil_moisture" in pool.keys
    assert "soil_moisture" not in pool.excluded


def test_slot_over_ask_limit_is_excluded(tea_config: ProcessConfig) -> None:
    """追问次数超过上限的槽位不再问，改由写入侧强制置 unknown。"""
    pool = select_candidate_slots(
        tea_config,
        _state(_ask_counters={"tea_symptom_part": 7}, _max_slot_rounds=6),
    )

    assert "tea_symptom_part" not in pool.keys
    assert pool.excluded["tea_symptom_part"] == "追问次数超限，转 unknown"


def test_slot_at_limit_is_kept(tea_config: ProcessConfig) -> None:
    """恰好等于上限仍可问：超限才剔除，边界不能提前一格。"""
    pool = select_candidate_slots(
        tea_config,
        _state(_ask_counters={"tea_symptom_part": 6}, _max_slot_rounds=6),
    )

    assert "tea_symptom_part" in pool.keys


# ---- 第三步：排序 ----


def test_required_slots_rank_before_optional(tea_config: ProcessConfig) -> None:
    """必问槽位排在非必问之前。"""
    pool = select_candidate_slots(tea_config, _state())

    required_flags = [c.required for c in pool.candidates]
    assert required_flags == sorted(required_flags, reverse=True), required_flags
    assert pool.candidates[0].reason == "must_ask"


def test_offline_questions_rank_up_when_no_env_data(tea_config: ProcessConfig) -> None:
    """`env_source="none"` 时增强追问入选，且理由标为 `offline_enhance`。"""
    pool = select_candidate_slots(tea_config, _state(observations={"env_source": "none"}))

    offline = [c for c in pool.candidates if c.reason == "offline_enhance"]
    assert [c.slot_key for c in offline] == ["tea_shade_level"]
    assert offline[0].relevant_score >= 40


def test_offline_questions_not_boosted_when_sensor_available(tea_config: ProcessConfig) -> None:
    """有环境数据时不给增强追问加分。

    槽位本身仍在 `required_slots` 里（配置声明了它，就一直是候选），但理由退回普通候选、
    分数也不再含 40 分加成——增强追问只在无传感器时才有信息增量。
    """
    with_sensor = select_candidate_slots(tea_config, _state(observations={"env_source": "sensor"}), top_k=20)
    without_sensor = select_candidate_slots(tea_config, _state(observations={"env_source": "none"}), top_k=20)

    scored = {c.slot_key: c for c in with_sensor.candidates}
    boosted = {c.slot_key: c for c in without_sensor.candidates}
    assert scored["tea_shade_level"].reason != "offline_enhance"
    assert boosted["tea_shade_level"].reason == "offline_enhance"
    assert boosted["tea_shade_level"].relevant_score == scored["tea_shade_level"].relevant_score + 40


def test_confusable_aware_slot_scores_higher(chili_config: ProcessConfig) -> None:
    """辣椒流程里 `chili_exudate` 的分数高于同等条件下的非鉴别槽位。"""
    pool = select_candidate_slots(chili_config, _state())

    scores = {c.slot_key: c.relevant_score for c in pool.candidates}
    assert scores["chili_exudate"] > scores["chili_soil_type"]
    assert scores["chili_exudate"] > scores["chili_greenhouse_humidity"]


def test_symptom_terms_boost_related_slot(chili_config: ProcessConfig) -> None:
    """农户已述症状命中槽位文本时加分：与当前描述相关的问题更可能有答案。"""
    quiet = select_candidate_slots(chili_config, _state())
    loud = select_candidate_slots(
        chili_config,
        _state(slots={"chili_greenhouse_humidity": _slot("chili_greenhouse_humidity", "filled", "萎蔫")}),
    )

    quiet_scores = {c.slot_key: c.relevant_score for c in quiet.candidates}
    loud_scores = {c.slot_key: c.relevant_score for c in loud.candidates}
    assert loud_scores["chili_symptom_pattern"] > quiet_scores["chili_symptom_pattern"]


def test_same_score_orders_by_ask_order() -> None:
    """分数相同时按 `ask_order` 升序：越小越先问。

    用去掉了易混淆项与选项的合成配置，让三项得分严格相同——否则相关性加分会把顺序
    搅乱，用例就测不到 `ask_order` 这一级排序。
    """
    config = get_process_config(ProcessId.TEA).model_copy(
        update={
            "confusable_items": [],
            "required_slots": [
                SlotSpec(
                    key=f"slot_{order}",
                    label="槽位",
                    display_label="槽位",
                    group=SlotGroup.MANAGEMENT,
                    required=True,
                    owner="interviewer",
                    ask_order=order,
                )
                for order in (30, 10, 20)
            ],
        }
    )

    pool = select_candidate_slots(config, _state())

    assert [c.slot_key for c in pool.candidates] == ["slot_10", "slot_20", "slot_30"]


def test_pool_is_reproducible(tea_config: ProcessConfig) -> None:
    """同一输入连续调用 100 次，候选序列完全一致（可重放的前提）。"""
    state = _state(observations={"env_source": "none"})

    first = [c.slot_key for c in select_candidate_slots(tea_config, state).candidates]
    for _ in range(100):
        assert [c.slot_key for c in select_candidate_slots(tea_config, state).candidates] == first


def test_selector_does_not_mutate_state(tea_config: ProcessConfig) -> None:
    """筛选器只读：`_ask_counters` 这类临时键不会被写回。"""
    counters = {"tea_symptom_part": 1}
    state = _state(_ask_counters=counters)

    select_candidate_slots(tea_config, state)

    assert counters == {"tea_symptom_part": 1}
    assert state["_ask_counters"] is counters  # type: ignore[typeddict-item]


# ---- 边界与签名 ----


def test_top_k_caps_candidates(tea_config: ProcessConfig) -> None:
    """`top_k=6` 时候选数不超过 6，超出部分直接截断。"""
    pool = select_candidate_slots(tea_config, _state())

    assert pool.top_k == 6
    assert len(pool.candidates) <= 6
    assert len(tea_config.required_slots) > 6, "样例流程的槽位应多于 top_k，否则本用例失去意义"


def test_selector_has_no_model_or_db_dependency() -> None:
    """签名里不得出现网关或数据库会话：筛选不依赖模型与数据库。"""
    params = inspect.signature(select_candidate_slots).parameters

    assert not {"gateway", "session", "db", "engine"} & set(params)
    assert list(params) == ["config", "state", "observations", "top_k"]


def test_pool_carries_process_id_and_reason(tea_config: ProcessConfig) -> None:
    """候选池带上流程标识与入选理由，便于线上排查"为什么问这个"。"""
    pool = select_candidate_slots(tea_config, _state())

    assert isinstance(pool, CandidatePool)
    assert pool.process_id is _TEA
    assert all(c.reason in ("must_ask", "offline_enhance", "relevant_followup") for c in pool.candidates)


def test_observed_facts_exclude_matching_slot(chili_config: ProcessConfig) -> None:
    """`observed_facts` 已有的键也算自动可得：对应槽位不必再问。"""
    observations = ObservationResult.model_validate(
        {
            "readings": [],
            "image_candidates": [ImageCandidate(label="叶片", score=0.9)],
            "data_available": True,
            "env_source": "sensor",
            "observed_facts": {"chili_soil_reading": "土温 26 度，含水正常"},
        }
    )

    pool = select_candidate_slots(chili_config, _state(), observations=observations)

    assert "chili_soil_reading" not in pool.keys
    assert pool.excluded["chili_soil_reading"] == "档案或环境已有"
