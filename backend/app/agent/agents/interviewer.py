"""问诊智能体：在代码给定的候选方向内生成追问（T2-16）。

它是"问五情"这一步的实现者，每轮产出最多两个追问。关键设计是**模型只在给定范围内选择
问题方向**：候选池由 T2-17 的筛选器在调用模型之前用纯代码算好，"跳过已知项"与"不漏必问项"
两个要求因此由代码保证，模型只负责把方向说得像人话。`validate_output` 是这条约束的执行
点——问题方向落在候选池之外即抛 `ContractViolation`，触发基类重试（带修复提示），重试仍
失败走模板降级。

三处不交给模型：

- **候选方向**：集合运算与排序，规则明确（T2-17 的筛选器）；
- **进度提示**（`compute_stage_hint`）：文案准确性直接决定农户对"还要问几轮"的预期，模型
  容易把它改写成"大概再问两三个问题"这类不准确表述；
- **数据缺失标注**（`compute_confidence_note`）：判据是 `env_source` 与 `unknown` 槽位数。

模板降级（`degrade`）必须能独立工作：模型不可用时仍要把问诊走完，因此 `text` 直接用配置
里的 `display_label`（已由配置作者口语化），不在降级路径上做任何语言加工——技术方案 10.3
的"模板必须本身可读，不能假设后续一定有模型润色"。

提示词构造已由 T2-60 迁移到 `app/agent/prompts/interviewer.py`，本模块只保留智能体行为：
候选池计算、候选域校验、进度提示与模板降级。`build_messages` 调用模板包的构造入口。
"""

from contextvars import ContextVar
from typing import Any, cast

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.agents.candidate_pool import CandidatePool, select_candidate_slots
from app.agent.agents.exceptions import AgentDegraded, ContractViolation
from app.agent.budget import ConsultBudget
from app.agent.contracts import InterviewBatch, InterviewQuestion
from app.agent.prompts.interviewer import TOP_K, build_interviewer_prompt
from app.agent.state import ConsultationState, TaskType
from app.llm import ModelGateway
from app.observability import log
from app.process_config.models import ProcessConfig, SlotSpec

# 模板问题的"为什么问"。按 `SlotGroup` 的值索引（配置里的中文分类），与 `SlotSpec.group`
# 一一对应；缺失分类回落到 `_DEFAULT_WHY`，新增槽位分类时不必同步改这里。
TEMPLATE_WHY: dict[str, str] = {
    "病虫情": "看清症状才能判断是哪一类问题",
    "管理": "近期农事与用药会影响判断",
    "种情": "播种和生育期决定当前该防什么",
    "肥情": "施肥情况会影响长势与抗性",
    "墒情": "土壤干湿关系到病害发生条件",
}
_DEFAULT_WHY = "了解基本情况才能给准建议"

# 降级说明。带"网络"字样是刻意的：它是农户能理解的原因，且与"没有环境数据"这类业务性
# 缺失区分开——后者由 `compute_confidence_note` 负责。
_DEGRADED_NOTE = "网络有点忙，先用固定问题问您几个关键的。"

# `unknown` 槽位累计到此数时主动说明把握程度。取三是工程经验值，调整时同步更新测试。
_UNKNOWN_NOTE_THRESHOLD = 3

_NOTE_NO_ENV = "暂时没有环境数据，下面的判断主要根据您描述的情况。"
_NOTE_STALE = "传感器数据不是最新的，判断可能有偏差。"
_NOTE_UNKNOWN = "有几项您不太确定，我会把把握程度说清楚。"

# 本轮算好的候选池经 contextvar 传给 `build_messages`：基类的 `build_messages` 是同步方法
# （T2-13 的契约，便于单测直接调用），而候选筛选是纯函数调用。用 contextvar 而不是实例
# 属性，是因为节点在多个会话间并发执行——asyncio 任务各自持有上下文副本，实例属性会被
# 互相覆盖。
_pool: ContextVar[CandidatePool | None] = ContextVar("interviewer_pool", default=None)


class InterviewerAgent(BaseAgent[InterviewBatch]):
    """问诊：在候选池范围内生成一到两个追问，进度提示与缺失标注由代码覆写。"""

    name = "interviewer"
    output_schema = InterviewBatch
    task_type = TaskType.INTERVIEW
    failure_mode = FailureMode.DEGRADE  # 降级为模板问题
    max_attempts = 2

    def __init__(self, gateway: ModelGateway, budget: ConsultBudget) -> None:
        super().__init__(gateway)
        self._budget = budget

    async def run(self, state: ConsultationState, ctx: AgentContext) -> InterviewBatch:
        """先算候选池（纯代码），再走基类的调用、校验与降级。

        候选池在 `run` 里算而不是在 `build_messages` 里：后者是同步方法，且会被重试反复
        调用，把纯函数计算放在入口更清楚。算出的结果写进 state 的临时键
        `_candidate_pool_keys`（下划线前缀表示不属于状态契约），供 `validate_output` 校验
        ——校验器因此不必重算，也不必依赖 `AgentContext` 携带流程外的运行数据。

        节点若已按同一筛选器预先写入该键，这里覆写为同值：`select_candidate_slots` 是确定
        性的纯函数，同一状态必然得到同一候选池，两处不会产生分歧。
        """
        pool = select_candidate_slots(ctx.process_config, state, top_k=TOP_K)
        cast(dict[str, Any], state)["_candidate_pool_keys"] = set(pool.keys)
        token = _pool.set(pool)
        try:
            return await super().run(state, ctx)
        finally:
            _pool.reset(token)

    # ---- 输入构造 ----

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词。候选池来自本轮 `run` 算出的结果，构造入口在模板包。"""
        pool = _pool.get() or select_candidate_slots(ctx.process_config, state, top_k=TOP_K)
        return build_interviewer_prompt(
            ctx.process_config,
            state,
            candidates=pool.candidates,
            region_code=ctx.region_code,
        )

    # ---- 候选域校验（模型只能在给定范围内选择） ----

    def validate_output(self, result: InterviewBatch, state: ConsultationState, ctx: AgentContext) -> None:
        """契约之外的校验：问题方向必须落在候选池内。

        这条校验是"模型只能在给定范围内选择"的执行点。它在 `validate_output` 里抛
        `ContractViolation`，从而触发基类的重试（带修复提示）；重试仍失败则走模板降级。
        """
        pool = cast(set[str], state.get("_candidate_pool_keys") or set())
        outside = [q.slot_key for q in result.questions if q.slot_key not in pool]
        if outside:
            raise ContractViolation(f"问了候选池之外的方向: {outside}")

    # ---- 进度提示由代码计算 ----

    def compute_stage_hint(self, state: ConsultationState) -> str:
        """进度提示：由代码按轮次计算，非模型生成。

        `round` 是已完成轮次，故当前轮为 `round + 1`；上界取 `max_interview_rounds`，避免
        在轮次用尽后的补问里出现"正在了解情况 6/4"这类越界文案。
        """
        current = min(state.get("round", 0) + 1, self._budget.max_interview_rounds)
        total = self._budget.max_interview_rounds
        return f"正在了解情况 {current}/{total}"

    # ---- 数据缺失标注 ----

    def compute_confidence_note(self, state: ConsultationState) -> str | None:
        """数据缺失或不确定性偏高时的显式标注。

        `unknown` 槽位累计到三个时给出提示，是「未知是合法答案」在产品层面的体现：系统不
        逼问，但要主动说明"因为不知道这些，结论的把握会低一些"。
        """
        observations = state.get("observations") or {}
        env_source = observations.get("env_source")
        if env_source == "none":
            return _NOTE_NO_ENV
        if env_source == "sensor_stale":
            return _NOTE_STALE
        unknown_slots = [key for key, slot in (state.get("slots") or {}).items() if slot.status == "unknown"]
        if len(unknown_slots) >= _UNKNOWN_NOTE_THRESHOLD:
            return _NOTE_UNKNOWN
        return None

    # ---- 契约之外的字段覆写 ----

    async def post_validate(
        self, result: InterviewBatch, state: ConsultationState, ctx: AgentContext
    ) -> InterviewBatch:
        """覆写进度提示与缺失标注，并记录本轮追问次数。

        进度提示一律由代码覆写：模型输出的 `stage_hint` 不可信（它会自由发挥成"再问两三个
        问题"），且该字段的准确性直接决定农户对剩余轮次的预期。`confidence_note` 只在代码
        判定需要标注时覆写——不需要标注时保留模型给的值，避免把"模型补充的说明"抹掉。
        """
        updates: dict[str, Any] = {"stage_hint": self.compute_stage_hint(state)}
        note = self.compute_confidence_note(state)
        if note:
            updates["confidence_note"] = note
        result = result.model_copy(update=updates)
        self._mark_asked_slots(state, result)
        return result

    def _mark_asked_slots(self, state: ConsultationState, batch: InterviewBatch) -> None:
        """记录本轮的追问次数；超限槽位将在写入时强制置 unknown。

        技术方案 7.3 规定"单槽位被追问超过上限仍未填，强制写 `status='unknown'`，不再
        追问"。强制写入的动作发生在槽位写入侧（T2-08 的 `write_slot` 配合预算检查），本
        智能体只负责计数；被标记为超限的槽位由 T2-17 筛选器的第二步读取本键后剔除，从而
        落实"不允许为了凑齐信息而反复逼问同一个问题"。
        """
        counters = dict(cast(dict[str, int], state.get("_ask_counters") or {}))
        for question in batch.questions:
            counters[question.slot_key] = counters.get(question.slot_key, 0) + 1
        cast(dict[str, Any], state)["_ask_counters"] = counters
        evictions = [key for key, count in counters.items() if count > self._budget.max_slot_rounds]
        if evictions:
            log.info("slot ask limit hit", extra={"slots": evictions})

    # ---- 模板问题降级 ----

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> InterviewBatch:
        """降级：按追问顺序取前两个必问槽位，用流程配置里的文案直接提问。

        `text` 直接用 `display_label` 而不加工：模板路径下没有模型可做润色，模板本身必须
        可读（技术方案 10.3）。
        """
        pool = self._template_pool(state, ctx.process_config)
        questions = [
            InterviewQuestion(
                slot_key=spec.key,
                text=spec.display_label,
                why=TEMPLATE_WHY.get(spec.group.value, _DEFAULT_WHY),
                options=spec.options,
                # 有选项就问选择（前端可渲染快捷按钮），没选项就只能让农户自己说。
                answer_type="single" if spec.options else "text",
            )
            for spec in pool[: self._budget.max_questions_per_round]
        ]
        if not questions:
            # 没有任何可问的必问槽位，说明该流程已收敛，应由主图推进到下一环节。这不是
            # 重试能解决的问题，故抛 `AgentDegraded` 而不是返回空批次（契约也不允许空）。
            raise AgentDegraded("无可用模板问题，应由主图推进到下一环节")
        return InterviewBatch(
            questions=questions,
            stage_hint=self.compute_stage_hint(state),
            confidence_note=_DEGRADED_NOTE,
        )

    def _template_pool(self, state: ConsultationState, config: ProcessConfig) -> list[SlotSpec]:
        """模板问题来源：必问且未填且未被超限剔除的槽位，按 `ask_order` 排序。"""
        counters = cast(dict[str, int], state.get("_ask_counters") or {})
        out: list[SlotSpec] = []
        for spec in sorted(config.required_slots, key=lambda s: s.ask_order):
            slot = (state.get("slots") or {}).get(spec.key)
            filled_or_answered = slot is not None and slot.status in ("filled", "unknown")
            if spec.required and not filled_or_answered and counters.get(spec.key, 0) <= self._budget.max_slot_rounds:
                out.append(spec)
        return out

    # ---- 无环境数据时的增强追问 ----

    def include_offline_questions(self, state: ConsultationState, config: ProcessConfig) -> list[SlotSpec]:
        """`env_source` 为 `none` 时把 `offline_questions` 的目标槽位纳入候选。

        返回的是槽位规格而不是问题文本：这些规格交给 T2-17 的筛选器与常规必问槽位合并后
        排序，两条来源因此走同一条筛选链路，不会出现两套逻辑。传感器数据陈旧
        （`sensor_stale`）不算"没有环境数据"——设备是装着的，该去排查设备，而不是改问
        "地里干湿如何"这类替代问题（与 T2-15 的 `offline_questions_needed` 同一判据）。
        """
        observations = state.get("observations") or {}
        if observations.get("env_source") != "none":
            return []
        specs: list[SlotSpec] = []
        for question in config.offline_questions:
            spec = config.slot_spec(question.slot_key)
            if spec is not None:
                specs.append(spec)
        return specs


__all__ = ["TEMPLATE_WHY", "InterviewerAgent"]
