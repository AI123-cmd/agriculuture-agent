"""T4-05 验收：问诊并列选项数量上限的裁剪与降级。

契约层的职责是"兜底裁剪而非报错"：模型返回超量选项时裁到上限并保留跳过项，与 T2-10
对 `questions` 裁至 2 的语义一致（多给一个选项的代价有限，不值得让整轮重试）。
全部用例用构造对象，不依赖模型与数据库。
"""

from app.agent.contracts import InterviewBatch, InterviewQuestion
from app.process_config.models import MAX_OPTIONS_PER_BATCH, MAX_OPTIONS_PER_QUESTION


def _question(slot_key: str, options: list[str], *, answer_type: str = "single") -> InterviewQuestion:
    return InterviewQuestion(
        slot_key=slot_key,
        text="是什么样子的斑？",
        why="不同斑形指向不同病害",
        answer_type=answer_type,
        options=options,
    )


# ---- 一、单问：裁到 4 项且保留跳过项 ----


def test_six_options_trimmed_to_four_keeping_skip() -> None:
    """6 项选项（跳过项在末位）裁为 4 项，跳过项保留，前 3 项为原顺序前 3 个。"""
    q = _question("k", ["圆形褐斑", "不规则黄斑", "水渍状", "边缘清晰", "有霉层", "说不清"])
    assert q.options == ["圆形褐斑", "不规则黄斑", "水渍状", "说不清"]
    assert len(q.options) == MAX_OPTIONS_PER_QUESTION


def test_exactly_four_options_not_trimmed() -> None:
    """正好 4 项时不裁剪。"""
    options = ["圆形褐斑", "不规则黄斑", "水渍状", "说不清"]
    q = _question("k", options)
    assert q.options == options


def test_skip_option_survives_when_ranked_last() -> None:
    """跳过项排在末位也不会被裁掉：农户答不出是合法答案（产品说明书 4.2.2）。"""
    q = _question("k", ["a", "b", "c", "d", "e", "说不清"])
    assert "说不清" in q.options


def test_batch_appends_skip_within_cap() -> None:
    """模型给满 4 项真实选项且漏了跳过项时，补位后仍是 4 项（不是 5 项）。"""
    batch = InterviewBatch(questions=[_question("k", ["a", "b", "c", "d"])], stage_hint="1/3")
    assert len(batch.questions[0].options) == MAX_OPTIONS_PER_QUESTION
    assert "说不清" in batch.questions[0].options


# ---- 二、一轮：总数收敛到 6 且首问完整 ----


def test_batch_two_questions_four_each_converges_to_six() -> None:
    """两问各 4 项时总选项收敛到 6，且首问选项完整。"""
    first = _question("k1", ["a", "b", "c", "说不清"])
    second = _question("k2", ["d", "e", "f", "说不清"])
    batch = InterviewBatch(questions=[first, second], stage_hint="1/3")

    assert batch.questions[0].options == ["a", "b", "c", "说不清"]
    total = sum(len(q.options or []) for q in batch.questions)
    assert total == MAX_OPTIONS_PER_BATCH


def test_batch_total_keeps_skip_on_trimmed_second_question() -> None:
    """第二问被总额度裁剪时同样保留跳过项。"""
    first = _question("k1", ["a", "b", "c", "说不清"])
    second = _question("k2", ["d", "e", "f", "说不清"])
    batch = InterviewBatch(questions=[first, second], stage_hint="1/3")

    assert "说不清" in batch.questions[1].options


def test_batch_within_limit_unchanged() -> None:
    """未超过总额度的一轮原样保留。"""
    batch = InterviewBatch(
        questions=[_question("k1", ["a", "b", "说不清"]), _question("k2", ["c", "d", "说不清"])],
        stage_hint="1/3",
    )
    assert batch.questions[0].options == ["a", "b", "说不清"]
    assert batch.questions[1].options == ["c", "d", "说不清"]
