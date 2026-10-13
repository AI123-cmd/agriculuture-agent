"""判定智能体：综合槽位与证据产出结论（T2-19）。

它是全链路自治程度最高的一环，也是准确性的核心。三条硬约束在这里由代码保证，而不是
靠提示词：

- **把握程度只能是枚举**：契约（T2-11）本身不含分数字段，提示词里再显式禁止百分比，
  形成双保险。数值型置信度只存在于 `internal_confidence` 一个方法内，写进
  `state["confidence_score"]` 供 T2-24 的条件边使用，绝不进入展示文案。
- **有易混淆项的流程必须显式鉴别**：`validate_output` 检查 `excluded` 是否覆盖了
  `confusable_items` 中 `must_exclude` 的候选（结论本身也算覆盖）。校验失败抛
  `ContractViolation` 触发重试（带修复提示），重试仍失败走不确定降级——降级比输出
  一个没做鉴别的确定结论安全：辣椒的青枯病与枯萎病处置方向完全不同，猜错的代价是无效用药。
- **不确定必须说明来源**：契约层已拦"既不说明来源也不说缺什么"的输出（T2-11），本模块
  再补一条由系统写入的来源——并行会诊未收敛（T2-20 写 `panel_force_uncertain`）。检索无
  命中**不**算作强制不确定的理由：判据在农业垂直领域模型内部，外部知识库是佐证而非前置。

`terminology_profile` 由代码从 `process_id` 填入而不是让模型生成：问诊已经在某流程内进行，
这个值就是确定的，让模型生成只会引入不一致。

提示词构造已由 T2-60 迁移到 `app/agent/prompts/diagnoser.py`，本模块只保留智能体行为：
排除项校验、不确定性来源补全、内部置信度与降级。`build_messages` 调用模板包的构造入口。
"""

from typing import Any, cast

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.agents.exceptions import ContractViolation
from app.agent.budget import ConsultBudget
from app.agent.contracts import DiagnosisResult
from app.agent.prompts.diagnoser import build_diagnoser_prompt
from app.agent.state import ConsultationState, TaskType
from app.llm import ModelGateway
from app.process_config.models import ProcessConfig

# 超边界原因。技术方案 6.7 要求超边界时向农户解释"为什么不能给建议"，这句话由代码给出，
# 不由模型生成——它是路由结果的说明，不是判断内容。
_BOUNDARY_REASON_DEFAULT = "该问题超出本流程的能力范围"

# 不确定且未给待补信息时的兜底。正常路径下契约（T2-11）已拦住这种输出，这里只是让
# `post_validate` 的重建在任何输入下都不会再次抛错。
_NEEDS_MORE_INFO_DEFAULT = ["建议补充症状照片或到农技站咨询"]

# `basis` 意外为空时的观察要点兜底。同上是防御性的：契约要求 `basis` 必填且逐条非空。
_DEFAULT_OBSERVATION_POINT = "记录症状出现时间与范围变化"


def _append_source(updates: dict[str, Any], result: DiagnosisResult, source: str) -> None:
    """把系统判定的不确定性来源追加到覆写集合里，保留模型自己给出的来源。

    不能直接 `setdefault(..., []).append(...)`：那样会在第一次追加时丢弃模型已经列出的
    来源，而"模型自报不确定"与"系统判定无证据"本就是两条要分开记录的来源。
    """
    existing = updates.get("uncertainty_source", result.uncertainty_source)
    updates["uncertainty_source"] = [*existing, source]


class DiagnoserAgent(BaseAgent[DiagnosisResult]):
    """判定：综合槽位与证据给结论，强制鉴别、强制说明不确定性来源。"""

    name = "diagnoser"
    output_schema = DiagnosisResult
    task_type = TaskType.DIAGNOSE
    failure_mode = FailureMode.DEGRADE  # 输出不确定加求助引导
    # 判定的每次重试都要跑主模型，成本最高，故与基类默认一致（总计最多 3 次调用）。
    max_attempts = 2

    def __init__(self, gateway: ModelGateway, budget: ConsultBudget) -> None:
        super().__init__(gateway)
        self._budget = budget

    # ---- 输入构造 ----

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词。证据取 state 里已核准的条目（`EvidenceItem` 序列化后的字典）。"""
        evidence = cast(list[dict], state.get("evidence") or [])
        return build_diagnoser_prompt(ctx.process_config, state, evidence)

    # ---- 排除项的业务校验 ----

    def required_exclusions(self, config: ProcessConfig) -> set[str]:
        """从易混淆项生成必须显式排除的候选集合。"""
        required: set[str] = set()
        for pair in config.confusable_items:
            if pair.must_exclude:
                required.update(pair.candidates)
        return required

    def validate_output(self, result: DiagnosisResult, state: ConsultationState, ctx: AgentContext) -> None:
        """业务校验：有易混淆项的流程必须显式排除。

        `missing` 的计算里减去了 `result.conclusion`：如果结论本身就是某个候选（如结论是
        "青枯病"），它不需要出现在排除项里，但另一个候选必须出现。技术方案 5.3.4 的表述是
        "强制输出排除项，使模型必须做鉴别而不是直接匹配"，因此校验的重点是"另一个候选有没有
        被排除"，而不是"所有候选都要出现在排除列表"。
        """
        required = self.required_exclusions(ctx.process_config)
        if not required:
            return
        if not result.excluded:
            raise ContractViolation("该流程有易混淆项，必须输出 excluded")
        missing = required - set(result.excluded) - {result.conclusion}
        if missing:
            raise ContractViolation(f"未显式排除: {sorted(missing)}")

    # ---- 契约之外的字段覆写 ----

    async def post_validate(
        self, result: DiagnosisResult, state: ConsultationState, ctx: AgentContext
    ) -> DiagnosisResult:
        """三处覆写：术语档、面板未收敛强制不确定、超边界补原因。

        `panel_force_uncertain` 由 T2-20 写入，与模型自身判断的 `uncertain` 分开记入
        `uncertainty_source`，让线上能区分"多视角未能收敛"与"模型自报不确定"。

        证据集为空**不再**强制不确定：本流程面向的是农业垂直领域模型，判据知识已在模型内部，
        外部知识库是佐证而非给出结论的前置条件。此前"检索为空即 uncertain"会让每一次问诊
        在知识库尚未灌数时都滚入回环、最终转人工。无佐证不等于判不了，此处尊重模型自身的
        把握程度。

        分数在这里写入 `state["confidence_score"]` 供 T2-24 的条件边使用。用原地写入与
        其余智能体一致；节点工厂需把该键一并作为返回值提交，否则 LangGraph 不会持久化它。
        """
        updates: dict[str, Any] = {}
        # 1. 术语档位由代码填入，不由模型自由生成。
        updates["terminology_profile"] = ctx.process_config.process_id.value
        # 2. 证据集仅用于内部置信度的微弱加成，不再改写 `confidence_level`。
        evidence = state.get("evidence") or []
        # 3. 面板未收敛时强制不确定（由 T2-20 写入 state 标记）。
        if bool(cast(dict[str, Any], state).get("panel_force_uncertain")):
            updates["confidence_level"] = "uncertain"
            _append_source(updates, result, "并行会诊未能唯一确定")
        # 4. 超边界时补齐原因。
        if result.out_of_scope and not result.boundary_reason:
            updates["boundary_reason"] = _BOUNDARY_REASON_DEFAULT
        merged = {**result.model_dump(), **updates}
        if (
            merged["confidence_level"] == "uncertain"
            and not merged["needs_more_info"]
            and not merged["uncertainty_source"]
        ):
            merged["needs_more_info"] = list(_NEEDS_MORE_INFO_DEFAULT)
        final = DiagnosisResult.model_validate(merged)
        cast(dict[str, Any], state)["confidence_score"] = self.internal_confidence(final, len(evidence))
        return final

    # ---- 内部置信度分数与展示的分离 ----

    def internal_confidence(self, result: DiagnosisResult, evidence_count: int) -> float:
        """内部置信度，仅供路由判断。不得进入任何契约字段或展示文案。

        分数的产生限制在这一个方法内，便于审计"哪些地方读了分数"。技术方案 5.3.4 明确
        "内部可以保留置信度分数用于路由判断，但不得进入展示文案"。
        """
        base = {"likely": 0.85, "preliminary": 0.6, "uncertain": 0.3}[result.confidence_level]
        evidence_bonus = min(0.1, 0.02 * evidence_count)
        return min(1.0, base + evidence_bonus)

    # ---- 需补信息触发回环 ----

    def should_loop_back(self, result: DiagnosisResult, state: ConsultationState) -> bool:
        """是否需要回到问诊补信息。受预算约束。

        两个上限都要检查：`max_judge_loops` 限制"置信度不足反向补问最多 2 次"，
        `max_interview_rounds` 限制总轮次不超过 4。两者由不同机制维护，不能假设一个蕴含
        另一个——若补问不消耗问诊轮次（在同一轮内多次取证），就需要 `max_judge_loops` 兜底。
        """
        if result.out_of_scope:
            return False
        if not result.needs_more_info:
            return False
        raw = cast(dict[str, Any], state)
        loops = cast(int, raw.get("judge_loops") or 0)
        rounds = cast(int, raw.get("round") or 0)
        return loops < self._budget.max_judge_loops and rounds < self._budget.max_interview_rounds

    # ---- 超边界的处置 ----

    def build_boundary_notice(self, result: DiagnosisResult, config: ProcessConfig) -> str:
        """超边界时的面向农户文案：建议咨询农技站加可自行观察的要点。

        "可自行观察的要点"从 `basis` 取前两条，因为 `basis` 是模型已经逐条列出的判断依据，
        它本身就是观察项的来源。这样文案的实质内容有出处，而不是重新生成一段泛泛的建议。
        """
        points = result.basis[:2] or [_DEFAULT_OBSERVATION_POINT]
        observations = "；".join(points)
        return (
            f"这个问题超出了我能给准建议的范围。建议带上这条信息去当地农技站：{observations}。"
            f"您也可以先观察{observations}，有明显变化时再来问我。"
        )

    # ---- 降级 ----

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> DiagnosisResult:
        """降级：输出不确定加求助引导。

        `excluded` 用 `required_exclusions` 的完整集合填充而不是留空：这个集合是配置给定的、
        与本次问诊内容无关的固定值，填上不构成任何编造；若留空，业务校验会再次失败，形成
        降级后又降级的循环。`out_of_scope=False` 是刻意的：判定服务不可用不等于问题超边界，
        超边界是关于问题内容的判断，服务不可用时无从判断。这样农户拿到的是一条"暂时给不出
        明确结论，建议观察或求助"的回复，而不是被直接推到人工通道。
        """
        return DiagnosisResult(
            conclusion="暂时无法给出明确结论",
            confidence_level="uncertain",
            basis=["判定服务暂时不可用"],
            uncertainty_source=[f"判定环节不可用（{reason}）"],
            excluded=list(self.required_exclusions(ctx.process_config)),
            terminology_profile=ctx.process_config.process_id.value,
            needs_more_info=["建议先观察植株变化，或携带症状照片到农技站咨询"],
            out_of_scope=False,
            boundary_reason=None,
        )


__all__ = ["DiagnoserAgent"]
