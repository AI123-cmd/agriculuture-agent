"""T2-10：问诊契约的五条规则。

五条规则里只有一部分能由字段结构执行（每轮不超过 2 问、必须解释为什么问、选项与作答形态
必须一致），它们在用例里是抛错或裁剪；另外两条（不重复问已知项、剔除可自动读取的项）需要
全量 `slots` 与流程配置，由 T2-17 的候选池筛选器在调用模型之前完成，**不在本契约里**，
因此这里也没有对应用例。
"""

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    SKIP_MARKERS,
    AnswerTypeValue,
    InterviewBatch,
    InterviewQuestion,
    QuestionOption,
    dump_contract,
    load_contract,
)
from app.agent.state import AnswerType


def _question(**overrides) -> dict:
    base = {
        "slot_key": "tea_leaf_symptom",
        "text": "叶片上是什么样子的斑？",
        "why": "不同病斑形状指向不同的病害，先确认样子能少走弯路",
        "answer_type": "single",
        "options": ["圆形褐斑", "不规则黄斑", "说不清"],
    }
    base.update(overrides)
    return base


def _batch(questions: list[dict], **overrides) -> dict:
    base = {"questions": questions, "stage_hint": "正在了解情况 1/3"}
    base.update(overrides)
    return base


def test_valid_batch_passes() -> None:
    batch = InterviewBatch(**_batch([_question()]))
    assert len(batch.questions) == 1
    assert batch.confidence_note is None
    assert batch.questions[0].slot_key == "tea_leaf_symptom"


def test_three_questions_trimmed_to_two() -> None:
    """规则一：每轮不超过 2 问。裁剪而不是抛错——多问一题的代价不值得让整轮重试。"""
    batch = InterviewBatch(
        **_batch(
            [
                _question(slot_key="k1"),
                _question(slot_key="k2"),
                _question(slot_key="k3"),
            ]
        )
    )
    assert len(batch.questions) == 2
    assert [q.slot_key for q in batch.questions] == ["k1", "k2"]


def test_two_questions_pass_unchanged() -> None:
    batch = InterviewBatch(**_batch([_question(slot_key="k1"), _question(slot_key="k2")]))
    assert len(batch.questions) == 2


def test_blank_why_raises() -> None:
    """规则二：必须解释为什么问。`why` 是必填且非空的字段，不能靠模型自律。"""
    with pytest.raises(ValidationError, match="不得为空"):
        InterviewQuestion(**_question(why="   "))


def test_missing_why_raises() -> None:
    payload = _question()
    payload.pop("why")
    with pytest.raises(ValidationError):
        InterviewQuestion(**payload)


def test_blank_text_raises() -> None:
    with pytest.raises(ValidationError, match="不得为空"):
        InterviewQuestion(**_question(text=""))


def test_text_is_stripped() -> None:
    q = InterviewQuestion(**_question(text="  叶片怎么了  ", why="  为了定位  "))
    assert q.text == "叶片怎么了"
    assert q.why == "为了定位"


def test_zero_questions_raises() -> None:
    """零问题意味着契约违约：该智能体本该转其他节点，静默通过会让主图空转一轮。"""
    with pytest.raises(ValidationError):
        InterviewBatch(**_batch([]))


def test_duplicate_slot_in_same_round_raises() -> None:
    with pytest.raises(ValidationError, match="同一轮不得重复询问同一槽位"):
        InterviewBatch(**_batch([_question(slot_key="k1"), _question(slot_key="k1")]))


def test_duplicate_slot_across_rounds_is_fine() -> None:
    """跨轮重复是合法的：农户上一轮跳过、这一轮再问一次是正常的追问策略。"""
    first = InterviewBatch(**_batch([_question(slot_key="k1")]))
    second = InterviewBatch(**_batch([_question(slot_key="k1")], stage_hint="正在了解情况 2/3"))
    assert first.questions[0].slot_key == second.questions[0].slot_key


def test_single_without_options_raises() -> None:
    """选了单选却没给选项会让前端无法渲染，必须在契约层拦住。"""
    with pytest.raises(ValidationError, match="必须给出选项"):
        InterviewQuestion(**_question(answer_type="single", options=None))


def test_multi_without_options_raises() -> None:
    with pytest.raises(ValidationError, match="必须给出选项"):
        InterviewQuestion(**_question(answer_type="multi", options=[]))


def test_text_type_without_options_passes() -> None:
    q = InterviewQuestion(**_question(answer_type="text", options=None))
    assert q.options is None


def test_skip_option_appended_when_missing() -> None:
    """规则五：未知是合法答案。缺跳过项时补位而不是抛错，因为漏加属于机械遗漏。"""
    batch = InterviewBatch(**_batch([_question(options=["圆形褐斑", "不规则黄斑"])]))
    assert "说不清" in batch.questions[0].options


def test_skip_option_keeps_original_order() -> None:
    """补位后原有选项顺序不变：农户看到的选择顺序不应在不同轮次之间跳动。"""
    batch = InterviewBatch(**_batch([_question(options=["圆形褐斑", "不规则黄斑"])]))
    assert batch.questions[0].options == ["圆形褐斑", "不规则黄斑", "说不清"]


@pytest.mark.parametrize("marker", sorted(SKIP_MARKERS))
def test_any_skip_marker_suppresses_append(marker: str) -> None:
    """任一跳过词在位就不再补位，避免出现两个语义相同的跳过项。"""
    batch = InterviewBatch(**_batch([_question(options=["圆形褐斑", marker])]))
    assert batch.questions[0].options == ["圆形褐斑", marker]


def test_criteria_case_appends_to_three_elements() -> None:
    """验收标准原句：`options=['是','否']` 经 `InterviewBatch` 包装后被补为三元素。"""
    q = InterviewQuestion(slot_key="k", text="t", why="w", answer_type="single", options=["是", "否"])
    batch = InterviewBatch(questions=[q], stage_hint="x")
    assert len(batch.questions[0].options) == 3
    assert batch.questions[0].options[2] == "说不清"


def test_json_schema_max_items_is_two() -> None:
    """验收标准：结构化的 `maxItems` 必须为 2，模型才会被硬限制在 2 问。"""
    schema = InterviewBatch.model_json_schema()
    assert schema["properties"]["questions"]["maxItems"] == 2
    assert schema["properties"]["questions"]["minItems"] == 1


def test_json_schema_generates_for_question() -> None:
    schema = InterviewQuestion.model_json_schema()
    assert set(schema["required"]) == {"slot_key", "text", "why", "answer_type"}


def test_answer_type_accepts_enum_member() -> None:
    """配置里的 `answer_type` 是 `AnswerType` 枚举，契约按技术方案原文标注为 `Literal`；
    两者必须在同一字段上共存，否则问诊模板无法直接构造契约对象。"""
    q = InterviewQuestion(**_question(answer_type=AnswerType.SINGLE))
    assert q.answer_type == "single"


def test_answer_type_rejects_unknown_value() -> None:
    """取值合法性仍由 `Literal` 判定，`_accept_enum_value` 不做第二套白名单。"""
    with pytest.raises(ValidationError):
        InterviewQuestion(**_question(answer_type="voice"))


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewBatch(**_batch([_question()], prompt_version=3))


def test_question_fields_match_spec() -> None:
    assert set(InterviewQuestion.model_fields) == {"slot_key", "text", "why", "options", "answer_type"}


def test_batch_fields_match_spec() -> None:
    assert set(InterviewBatch.model_fields) == {"questions", "stage_hint", "confidence_note"}


def test_question_option_is_str_alias() -> None:
    """技术方案 5.3.3 把 `options` 定义为 `list[str]`，别名只做概念区分，不引入结构。"""
    assert QuestionOption is str
    assert AnswerTypeValue is not None


def test_confidence_note_carries_degradation() -> None:
    batch = InterviewBatch(**_batch([_question()], confidence_note="暂无传感器数据，判断基于你的描述"))
    assert batch.confidence_note is not None


def test_roundtrip_through_state_payload() -> None:
    original = InterviewBatch(**_batch([_question()], confidence_note="暂无环境数据"))
    restored = load_contract(InterviewBatch, dump_contract(original))
    assert restored == original
