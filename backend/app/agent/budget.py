"""轮次与成本预算守卫：把四个轮次上限与一个 token 预算收进一个可单测的对象（T2-31）。

替换原有实现里的单一 `round` 计数（阈值来自全局配置项）：那个
阈值是全局共享的可变状态，重启才能改，且无法按会话追溯。收进 `ConsultBudget` 之后
有两个直接收益——它可被写进检查点，历史问诊能复现"当时用了多少轮、花了多少 token"；
也可按流程差异化，而无需动全局配置。

路由函数只读 `state["budget"]` 字典的键，不重建对象，保证纯函数与可重放；节点内需要
记账时从 state 反序列化出对象、改完再写回（`ConsultBudget(**state["budget"])`）。
"""

from enum import Enum

from pydantic import BaseModel, Field


class BudgetExceeded(str, Enum):
    """超预算的类型，供路由与日志区分处理方式。"""

    NONE = "none"
    INTERVIEW_ROUNDS = "interview_rounds"  # 问诊轮次用尽仍未收敛 -> 转人工
    SLOT_ROUNDS = "slot_rounds"  # 单槽位追问超限 -> 强制写 unknown
    JUDGE_LOOPS = "judge_loops"  # 判定回环用尽 -> 转人工
    TOKENS = "tokens"  # token 超预算 -> 降级小模型继续


class ConsultBudget(BaseModel):
    """单次问诊的预算：上限 + 已用量。每次节点推进后调用记账方法。"""

    # 上限（来自技术方案 7.3，均为产品说明书要求的硬约束）
    max_interview_rounds: int = 4  # 产品说明书 2.3：收敛轮次目标 <= 4
    max_judge_loops: int = 2  # 4.2.2：置信度不足反向补问最多 2 次
    max_questions_per_round: int = 2  # 4.2.2：每轮追问不超过 2 个问题
    max_slot_rounds: int = 6  # 单槽位重复追问上限，超限写 unknown
    max_tokens_per_consult: int = 24000  # 单次问诊 token 预算

    # 已用量
    interview_rounds: int = 0
    judge_loops: int = 0
    questions_this_round: int = 0
    slot_rounds: dict[str, int] = Field(default_factory=dict)
    tokens_used: int = 0

    @classmethod
    def initial(cls) -> "ConsultBudget":
        """会话开始时的初始预算，由 intake 节点调用。"""
        return cls()

    # ---- 记账 ----

    def note_interview_round(self) -> None:
        """一轮问诊结束：轮次 +1，本轮问题数清零。"""
        self.interview_rounds += 1
        self.questions_this_round = 0

    def note_question(self, slot_key: str) -> None:
        """提出一个问题：本轮问题数 +1，该槽位追问次数 +1。"""
        self.questions_this_round += 1
        self.slot_rounds[slot_key] = self.slot_rounds.get(slot_key, 0) + 1

    def note_judge_loop(self) -> None:
        """判定要求补问一次：回环计数 +1。"""
        self.judge_loops += 1

    def note_tokens(self, count: int) -> None:
        """模型网关每次调用后回写 token 用量。"""
        self.tokens_used += count

    # ---- 守卫 ----

    def check_interview_rounds(self) -> BudgetExceeded:
        if self.interview_rounds >= self.max_interview_rounds:
            return BudgetExceeded.INTERVIEW_ROUNDS
        return BudgetExceeded.NONE

    def check_judge_loops(self) -> BudgetExceeded:
        if self.judge_loops >= self.max_judge_loops:
            return BudgetExceeded.JUDGE_LOOPS
        return BudgetExceeded.NONE

    def check_question_quota(self) -> bool:
        """本轮是否还能再问一个问题（每轮 <= 2 问）。"""
        return self.questions_this_round < self.max_questions_per_round

    def check_slot(self, slot_key: str) -> BudgetExceeded:
        """单槽位是否已被追问超过上限（超过即应写 unknown，不再追问）。"""
        if self.slot_rounds.get(slot_key, 0) >= self.max_slot_rounds:
            return BudgetExceeded.SLOT_ROUNDS
        return BudgetExceeded.NONE

    def check_tokens(self) -> BudgetExceeded:
        if self.tokens_used >= self.max_tokens_per_consult:
            return BudgetExceeded.TOKENS
        return BudgetExceeded.NONE

    def to_state(self) -> dict:
        """序列化进 ConsultationState；路由函数按这些键名读取。"""
        return self.model_dump()


__all__ = ["BudgetExceeded", "ConsultBudget"]
