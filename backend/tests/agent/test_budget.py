"""T2-31：`ConsultBudget` 的五个上限与三类超预算判定。

预算对象是纯逻辑模块（不碰数据库/Redis/模型），所以这里全部是边界测试：每个上限的判定
用 `>=`，因此"刚好用尽"与"用尽之后"必须分开断言——`note_*` 第 N 次与第 N-1 次的结果差异
正是路由与问诊节点据以分支的依据。同时固定 `to_state()` 的 round-trip，保证写进检查点的
字典能被 `ConsultBudget(**state["budget"])` 无损还原（否则历史问诊复现时会丢计数）。
"""

import pytest

from app.agent.budget import BudgetExceeded, ConsultBudget


def test_defaults_match_spec() -> None:
    """五个上限取方案 7.3 的硬约束值；已用量全部从 0 起算。"""
    budget = ConsultBudget()
    assert budget.max_interview_rounds == 4
    assert budget.max_judge_loops == 2
    assert budget.max_questions_per_round == 2
    assert budget.max_slot_rounds == 6
    assert budget.max_tokens_per_consult == 24000

    assert budget.interview_rounds == 0
    assert budget.judge_loops == 0
    assert budget.questions_this_round == 0
    assert budget.slot_rounds == {}
    assert budget.tokens_used == 0
    assert budget.check_interview_rounds() == BudgetExceeded.NONE
    assert budget.check_judge_loops() == BudgetExceeded.NONE
    assert budget.check_question_quota() is True
    assert budget.check_tokens() == BudgetExceeded.NONE


def test_initial_equals_fresh_instance() -> None:
    """`initial()` 是 intake 节点的入口，必须与直接构造等价。"""
    assert ConsultBudget.initial().to_state() == ConsultBudget().to_state()


# ---- 每轮问题配额 ----


def test_question_quota_blocks_after_second_question() -> None:
    """每轮 ≤ 2 问：第 2 次后配额用尽，第 3 次仍会被记账但不该再生成问题。"""
    budget = ConsultBudget()
    budget.note_question("symptom")
    assert budget.check_question_quota() is True

    budget.note_question("stage")
    assert budget.questions_this_round == 2
    assert budget.check_question_quota() is False

    budget.note_question("water")
    assert budget.questions_this_round == 3
    assert budget.check_question_quota() is False


def test_note_interview_round_resets_question_quota() -> None:
    """一轮结束后重置本轮问题数，否则第二轮的配额会被上一轮吃掉。"""
    budget = ConsultBudget()
    budget.note_question("symptom")
    budget.note_question("stage")
    assert budget.check_question_quota() is False

    budget.note_interview_round()
    assert budget.questions_this_round == 0
    assert budget.check_question_quota() is True


# ---- 单槽位追问上限 ----


def test_slot_rounds_hit_on_sixth_ask() -> None:
    """单槽位第 6 次追问才超限；第 5 次仍可继续问（不提前误杀）。"""
    budget = ConsultBudget()
    for _ in range(5):
        budget.note_question("symptom")

    assert budget.slot_rounds["symptom"] == 5
    assert budget.check_slot("symptom") == BudgetExceeded.NONE

    budget.note_question("symptom")
    assert budget.slot_rounds["symptom"] == 6
    assert budget.check_slot("symptom") == BudgetExceeded.SLOT_ROUNDS


def test_slot_rounds_are_tracked_per_key() -> None:
    """追问次数按槽位独立计数：一个槽位超限不影响另一个。"""
    budget = ConsultBudget()
    for _ in range(6):
        budget.note_question("symptom")
    budget.note_question("water")

    assert budget.check_slot("symptom") == BudgetExceeded.SLOT_ROUNDS
    assert budget.check_slot("water") == BudgetExceeded.NONE


def test_unknown_slot_is_within_budget() -> None:
    """没被追问过的槽位不在字典里，不能当成超限。"""
    assert ConsultBudget().check_slot("never_asked") == BudgetExceeded.NONE


# ---- 问诊轮次上限 ----


def test_interview_rounds_hit_on_fourth_round() -> None:
    budget = ConsultBudget()
    for _ in range(3):
        budget.note_interview_round()
    assert budget.check_interview_rounds() == BudgetExceeded.NONE

    budget.note_interview_round()
    assert budget.interview_rounds == 4
    assert budget.check_interview_rounds() == BudgetExceeded.INTERVIEW_ROUNDS


# ---- 判定回环上限 ----


def test_judge_loops_hit_on_second_loop() -> None:
    budget = ConsultBudget()
    budget.note_judge_loop()
    assert budget.check_judge_loops() == BudgetExceeded.NONE

    budget.note_judge_loop()
    assert budget.judge_loops == 2
    assert budget.check_judge_loops() == BudgetExceeded.JUDGE_LOOPS


# ---- token 预算 ----


def test_tokens_hit_at_budget() -> None:
    budget = ConsultBudget()
    budget.note_tokens(23999)
    assert budget.check_tokens() == BudgetExceeded.NONE

    budget.note_tokens(1)
    assert budget.tokens_used == 24000
    assert budget.check_tokens() == BudgetExceeded.TOKENS


def test_note_tokens_accumulates() -> None:
    """网关每次调用后回写增量，不是覆盖。"""
    budget = ConsultBudget()
    budget.note_tokens(100)
    budget.note_tokens(250)
    assert budget.tokens_used == 350


# ---- 序列化 ----


def test_to_state_round_trip() -> None:
    """写进检查点的字典必须能无损还原，否则历史问诊会丢计数。"""
    budget = ConsultBudget()
    budget.note_interview_round()
    budget.note_question("symptom")
    budget.note_question("symptom")
    budget.note_judge_loop()
    budget.note_judge_loop()
    budget.note_tokens(24000)

    state = budget.to_state()
    assert state == budget.model_dump()
    assert ConsultBudget(**state) == budget

    restored = ConsultBudget(**state)
    assert restored.check_interview_rounds() == BudgetExceeded.NONE
    assert restored.check_slot("symptom") == BudgetExceeded.NONE
    assert restored.check_judge_loops() == BudgetExceeded.JUDGE_LOOPS
    assert restored.check_tokens() == BudgetExceeded.TOKENS


def test_budget_exceeded_values_are_stable_strings() -> None:
    """枚举值被日志与路由分支引用，取值属对外契约，不得随意改动。"""
    assert BudgetExceeded.NONE == "none"
    assert BudgetExceeded.INTERVIEW_ROUNDS == "interview_rounds"
    assert BudgetExceeded.SLOT_ROUNDS == "slot_rounds"
    assert BudgetExceeded.JUDGE_LOOPS == "judge_loops"
    assert BudgetExceeded.TOKENS == "tokens"


@pytest.mark.parametrize("field", ["max_interview_rounds", "max_judge_loops", "max_slot_rounds"])
def test_limits_are_configurable_per_flow(field: str) -> None:
    """收进对象而非全局配置的意义：可按流程覆写阈值。"""
    budget = ConsultBudget(**{field: 1})
    assert getattr(budget, field) == 1
