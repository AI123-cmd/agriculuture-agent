"""T2-08：黑板三条规则与 `SlotValue` 状态一致性。

黑板的三条规则（单一写入者、只写不覆盖、带状态写入）都必须是代码而非提示词约定，
因此这里逐条构造正例与反例。用真实的茶流程配置：槽位声明与 `owner` 必须来自配置，
否则测出来的只是"函数按测试数据行事"，而不是"函数按配置行事"。
"""

import pytest
from pydantic import ValidationError

from app.agent.blackboard import (
    BlackboardError,
    append_evidence,
    read_slot,
    set_stage,
    slot_summary,
    write_slot,
)
from app.agent.state import ConsultStage, EvidenceItem, ProcessId, SlotValue
from app.process_config import ProcessConfig
from app.process_config.loader import load_process_config

_INTERVIEW_SLOT = "tea_symptom_part"  # owner: interviewer
_OBSERVER_SLOT = "tea_leaf_symptom_photo"  # owner: field_observer


@pytest.fixture()
def tea_config() -> ProcessConfig:
    return load_process_config(ProcessId.TEA)


def _slot(key: str, value: str | None = "叶尖焦枯", *, source: str = "user_answer", **extra) -> SlotValue:
    return SlotValue(key=key, value=value, status="filled" if value is not None else "missing", source=source, **extra)


def test_owner_can_write(tea_config) -> None:
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT), tea_config, writer="interviewer")
    assert state["slots"][_INTERVIEW_SLOT].value == "叶尖焦枯"


def test_non_owner_write_raises(tea_config) -> None:
    """单一写入者：现场采集的槽位不许问诊智能体写，反之亦然。"""
    state: dict = {}
    with pytest.raises(BlackboardError, match="无权写入"):
        write_slot(state, _slot(_OBSERVER_SLOT), tea_config, writer="interviewer")
    assert state.get("slots", {}) == {}


def test_undeclared_slot_raises(tea_config) -> None:
    """未在配置里声明的槽位没有 `owner` 可校验，必须直接拒绝。"""
    with pytest.raises(BlackboardError, match="未在流程 tea 中声明"):
        write_slot({}, _slot("not_declared_slot"), tea_config, writer="interviewer")


def test_lower_priority_source_cannot_overwrite_filled(tea_config) -> None:
    """`inferred` 永远是优先级最低的来源，不得覆盖档案事实。"""
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT, "档案里的答案", source="profile"), tea_config, writer="interviewer")
    write_slot(state, _slot(_INTERVIEW_SLOT, "模型推算的答案", source="inferred"), tea_config, writer="interviewer")

    assert state["slots"][_INTERVIEW_SLOT].value == "档案里的答案"
    assert state["slots"][_INTERVIEW_SLOT].source == "profile"


def test_same_priority_cannot_overwrite_filled(tea_config) -> None:
    """同优先级不得覆盖：否则同一轮重试会把先到的答案换掉，结果不可复现。"""
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT, "第一次", source="user_answer"), tea_config, writer="interviewer")
    write_slot(state, _slot(_INTERVIEW_SLOT, "第二次", source="user_answer"), tea_config, writer="interviewer")

    assert state["slots"][_INTERVIEW_SLOT].value == "第一次"


def test_higher_priority_source_can_overwrite_filled(tea_config) -> None:
    """高优先级可覆盖：档案来源高于农户口述，属正常业务路径（技术方案 6.3）。"""
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT, "农户口述", source="user_answer"), tea_config, writer="interviewer")
    write_slot(state, _slot(_INTERVIEW_SLOT, "档案登记", source="profile"), tea_config, writer="interviewer")

    assert state["slots"][_INTERVIEW_SLOT].value == "档案登记"


def test_missing_status_is_overwritable(tea_config) -> None:
    """已有槽位不是 `filled` 时不受优先级限制：还没问过，任何来源都能填。"""
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT, None, source="user_answer"), tea_config, writer="interviewer")
    write_slot(state, _slot(_INTERVIEW_SLOT, "模型推算", source="inferred"), tea_config, writer="interviewer")

    assert state["slots"][_INTERVIEW_SLOT].value == "模型推算"


def test_plot_prefix_keys_slot(tea_config) -> None:
    """多地块（技术方案 6.3）：键加地块前缀，槽位规格仍按未加前缀的 key 查。"""
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT), tea_config, writer="interviewer", plot_prefix="plot_1")

    assert set(state["slots"]) == {"plot_1.tea_symptom_part"}
    assert state["slots"]["plot_1.tea_symptom_part"].key == "plot_1.tea_symptom_part"
    assert read_slot(state, _INTERVIEW_SLOT, plot_prefix="plot_1") is state["slots"]["plot_1.tea_symptom_part"]


def test_same_slot_on_two_plots_does_not_collide(tea_config) -> None:
    """两个地块的同名槽位互不覆盖，优先级比较只在同一键内进行。"""
    state: dict = {}
    write_slot(state, _slot(_INTERVIEW_SLOT, "一号地"), tea_config, writer="interviewer", plot_prefix="plot_1")
    write_slot(state, _slot(_INTERVIEW_SLOT, "二号地"), tea_config, writer="interviewer", plot_prefix="plot_2")

    assert read_slot(state, _INTERVIEW_SLOT, plot_prefix="plot_1").value == "一号地"
    assert read_slot(state, _INTERVIEW_SLOT, plot_prefix="plot_2").value == "二号地"


def test_read_slot_returns_none_when_absent(tea_config) -> None:
    assert read_slot({}, _INTERVIEW_SLOT) is None


def test_slot_summary_skips_missing_and_marks_unknown() -> None:
    """`missing` 不进提示词（还没问过的项不该出现在上下文里）；`unknown` 用固定文案。"""
    state = {
        "slots": {
            "a": SlotValue(key="a", value="叶片发黄", status="filled", source="user_answer"),
            "b": SlotValue(key="b", status="unknown", source="user_answer"),
            "c": SlotValue(key="c", status="missing", source="user_answer"),
        }
    }
    assert slot_summary(state) == {"a": "叶片发黄", "b": "农户表示不清楚"}


def test_set_stage_returns_increment() -> None:
    """环节推进以返回值形式提交：原地改会让 checkpointer 与图的实际走向不一致。"""
    state: dict = {}
    assert set_stage(state, ConsultStage.OBSERVE) == {"stage": ConsultStage.OBSERVE}
    assert state == {}


def test_append_evidence_dedupes_by_knowledge_id() -> None:
    """同一轮重试会重复检索同一批条目，不去重会让重排与引用计数失真。"""
    item = EvidenceItem(
        knowledge_id="tea-pest-001",
        title="茶小绿叶蝉",
        content="若虫刺吸嫩梢汁液",
        process_id="tea",
        region_scope="province",
        effective_date=None,
        kind="pest",
        score=0.8,
    )
    first = append_evidence({}, [item])
    second = append_evidence({"evidence": first["evidence"]}, [item])

    assert len(first["evidence"]) == 1
    assert second["evidence"] == first["evidence"]
    assert second["evidence"][0]["knowledge_id"] == "tea-pest-001"


def test_append_evidence_keeps_distinct_items() -> None:
    def _item(kid: str) -> EvidenceItem:
        return EvidenceItem(
            knowledge_id=kid,
            title="t",
            content="c",
            process_id="tea",
            region_scope=None,
            effective_date=None,
            kind="disease",
            score=0.5,
        )

    out = append_evidence({}, [_item("a"), _item("b")])
    assert [e["knowledge_id"] for e in out["evidence"]] == ["a", "b"]


def test_slot_value_filled_requires_value() -> None:
    with pytest.raises(ValidationError):
        SlotValue(key="a", status="filled", source="profile")


def test_slot_value_missing_forbids_value() -> None:
    with pytest.raises(ValidationError):
        SlotValue(key="a", status="missing", value="x", source="profile")


def test_slot_value_unknown_is_legal_answer() -> None:
    """`unknown` 是合法答案：问过且农户答不出，不携带值。"""
    slot = SlotValue(key="a", status="unknown", source="user_answer", asked_at_round=1)
    assert slot.value is None


def test_slot_value_rejects_unknown_source() -> None:
    """枚举封闭：来源是封闭取值集合，模型输出的自由字符串不得直接进来。"""
    with pytest.raises(ValidationError):
        SlotValue(key="a", value="x", status="filled", source="guessed")


def test_slot_value_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        SlotValue(key="a", value="x", status="filled", source="profile", extra="nope")
