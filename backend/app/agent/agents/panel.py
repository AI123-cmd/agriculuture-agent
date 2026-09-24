"""受限并行会诊与纯规则聚合器（T2-20）。

对易混淆项鉴别以 fork-join 结构并行发起三路视角（症状学、环境与农事、排除项），再由
`aggregate_panel` 裁定。启用范围限茶、辣椒、杂草三条流程（`config.panel_enabled`），这是
技术方案 6.4 明确的范围限定。

**聚合器不投票**。三个视角各说一个病名然后取多数票，在只有三个视角时会退化为主观权重；
真正的鉴别依据是流程配置里写明的决定性特征，这类特征是排除性的、可验证的，必须优先于
统计式的意见聚合。三段逻辑依次尝试：

1. **硬判据优先**：有视角命中配置中的决定性特征时取该视角，多视角一致命中取同一候选；
2. **排除项优先**：被排除视角排除的候选直接出局，不论其他视角支持度；
3. **仍不唯一**：标记 `force_uncertain`，不做强行择一。

`force_uncertain=True` 由 T2-19 的判定智能体读取 `state["panel_force_uncertain"]` 后强制
覆写 `confidence_level`；面板本身不产出 `DiagnosisResult`，只产出意见与裁定结果。

成本控制的三条限制：只有启用流程才跑（`collect` 对 `panel_enabled=False` 直接返回空）、
仅鉴别节点触发一次（`already_ran` 供调用方查重）、聚合器为纯规则无模型消耗
（`aggregate_panel` 的签名里没有网关）。

提示词构造留在本模块内（公开的 `build_panel_prompt`），与 T2-14/15/16/18/19 一致，
由 T2-60 统一迁移到模板目录。
"""

import asyncio
import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from app.agent.agents.base import AgentContext
from app.agent.blackboard import slot_summary
from app.agent.state import ConsultationState, TaskType
from app.llm import ModelGateway
from app.llm.enums import TaskType as GatewayTaskType
from app.observability import log
from app.process_config.models import ProcessConfig

Perspective = Literal["symptom", "environment", "exclusion"]

# 三路视角及其关注点。提示词必须**互不覆盖**，否则三路会给出几乎相同的意见，并行就失去
# 意义。与技术方案 6.4 流程图里标注的每路关注点一一对应。
PERSPECTIVE_FOCUS: dict[str, str] = {
    "symptom": "只从症状学角度分析：病斑形态、受害部位、发展速度。不要考虑天气与用药。",
    "environment": "只从环境与农事角度分析：近期天气、用药史、栽培管理。不要考虑病斑形态。",
    "exclusion": "只从排除角度分析：能否排除生理性、药害、缺素等非侵染因素。给不出候选是正常的。",
}

# 并发视角的固定顺序。它是结果顺序的来源，不是集合——`all_candidates` 依赖保序去重，
# 顺序变了 `candidates` 也跟着变，可复现性就没了。
PERSPECTIVES: tuple[str, ...] = ("symptom", "environment", "exclusion")

# 注入提示词的证据条数上限，与技术方案 10.4 的上下文裁剪要求一致。
_MAX_EVIDENCE_ROWS = 5
_CONTENT_CAP = 100


class PanelOpinion(BaseModel):
    """单路视角的意见。

    `decisive_feature_hit` 存命中特征的**原文**而不是布尔值：判定"是否命中决定性特征"这件事
    因此可被审计——排查时能看到模型到底引用了哪句话，从而判断是判据配置有问题还是模型理解有偏差。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    perspective: Literal["symptom", "environment", "exclusion"]
    candidate: str | None  # 排除视角可为 None
    support: list[str]
    excluded_candidates: list[str] = []
    decisive_feature_hit: str | None = None
    confidence: Literal["likely", "preliminary", "uncertain"] = "preliminary"


class PanelAggregate(BaseModel):
    """聚合结果。`force_uncertain=True` 表示不做强行择一。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    winner: PanelOpinion | None = None
    candidates: list[str] = []
    resolution: Literal["decisive_feature", "elimination", "unresolved"]
    force_uncertain: bool = False
    excluded: list[str] = []
    rationale: str = ""


def aggregate_panel(opinions: list[PanelOpinion], config: ProcessConfig) -> PanelAggregate:
    """并行意见聚合：不投票，按流程配置的判据裁定。

    三段逻辑依次尝试，命中即返回。签名里没有网关与会话：聚合是纯规则，每次输入相同则输出
    完全相同（T2-20 的验收标准要求连续调用 100 次各字段一致）。裁定结果进展示与审计，因此
    `candidates` 保序去重而不是用集合。
    """
    if not opinions:
        return PanelAggregate(resolution="unresolved", force_uncertain=True, rationale="无有效视角意见")
    # 1. 硬判据优先：唯一视角命中流程配置中的"决定性特征"时，取该视角。
    decisive = [o for o in opinions if o.decisive_feature_hit]
    decisive_candidates = {o.candidate for o in decisive if o.candidate}
    if len(decisive) == 1:
        return PanelAggregate(
            winner=decisive[0],
            candidates=[decisive[0].candidate] if decisive[0].candidate else [],
            resolution="decisive_feature",
            excluded=_collect_excluded(opinions),
            rationale=f"命中决定性特征：{decisive[0].decisive_feature_hit}",
        )
    if len(decisive_candidates) == 1:
        winner = next(o for o in decisive if o.candidate in decisive_candidates)
        return PanelAggregate(
            winner=winner,
            candidates=[winner.candidate] if winner.candidate else [],
            resolution="decisive_feature",
            excluded=_collect_excluded(opinions),
            rationale=f"多视角一致命中决定性特征：{winner.decisive_feature_hit}",
        )
    # 2. 排除项优先：排除项视角（视角 C）若能明确排除某候选，该候选直接出局，
    #    不论其他视角支持度。
    excluded = reduce_exclusions(opinions, config)
    remaining = [c for c in all_candidates(opinions) if c not in excluded]
    # 3. 仍不唯一 → 标记为分歧，交判定智能体在结论中显式说明不确定性。
    if len(remaining) != 1:
        return PanelAggregate(
            candidates=remaining,
            resolution="unresolved",
            force_uncertain=True,
            excluded=excluded,
            rationale="并行会诊未收敛" if remaining else "全部候选被排除，需重新收集证据",
        )
    return PanelAggregate(
        winner=_opinion_for(opinions, remaining[0]),
        candidates=remaining,
        resolution="elimination",
        excluded=excluded,
        rationale=f"经排除后唯一候选：{remaining[0]}",
    )


def reduce_exclusions(opinions: list[PanelOpinion], config: ProcessConfig) -> list[str]:
    """汇总被排除的候选。仅接受有配置判据支撑的排除。

    若某视角排除了一个不在任何 `confusable_items` 候选集里的名称，这个排除被忽略：排除的
    效力来自配置中的决定性特征，没有配置支撑的排除只是模型的随口判断。这防止模型排除掉
    正确候选从而把结论引向错误方向。
    """
    configured = {c for pair in config.confusable_items for c in pair.candidates}
    excluded: list[str] = []
    for opinion in opinions:
        for cand in opinion.excluded_candidates:
            if cand in configured and cand not in excluded:
                excluded.append(cand)
    return excluded


def all_candidates(opinions: list[PanelOpinion]) -> list[str]:
    """收集所有视角提到的候选，保序去重。"""
    seen: list[str] = []
    for opinion in opinions:
        if opinion.candidate and opinion.candidate not in seen:
            seen.append(opinion.candidate)
    return seen


def _opinion_for(opinions: list[PanelOpinion], candidate: str) -> PanelOpinion:
    """取给出该候选的第一条意见。排除视角不给候选，故这里不会拿到 `None`。"""
    return next(o for o in opinions if o.candidate == candidate)


def _collect_excluded(opinions: list[PanelOpinion]) -> list[str]:
    """硬判据分支下汇总排除项。

    不过滤"配置里有判据的"——能走到硬判据分支说明候选域本身已被配置限定，此时保留模型给出的
    全部排除项是为了让展示层能完整交代"另外还排除了什么"。真正起裁定作用的排除走
    `reduce_exclusions`。
    """
    out: list[str] = []
    for opinion in opinions:
        for cand in opinion.excluded_candidates:
            if cand not in out:
                out.append(cand)
    return out


def build_panel_prompt(
    perspective: str,
    config: ProcessConfig,
    state: ConsultationState,
    ctx: AgentContext,
) -> list[dict]:
    """单路视角的提示词。视角差异全在 `PERSPECTIVE_FOCUS` 那一段。

    候选域与鉴别特征从配置注入：面板要在给定的候选里鉴别，不能让模型自己扩展候选集合。
    明确要求"若认为某候选可排除，在 `excluded_candidates` 中列出"，这是第二段裁定逻辑
    的数据来源。
    """
    focus = PERSPECTIVE_FOCUS.get(perspective, PERSPECTIVE_FOCUS["symptom"])
    system = [
        f"你是{config.role_definition}，在并行会诊中负责一个视角。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(PanelOpinion.model_json_schema(), ensure_ascii=False)}",
        focus,
        f"待鉴别的候选只能从下列名称中选择：{_candidate_names(config)}",
        f"各候选的决定性特征：{_format_features(config)}",
        f"使用术语：{'、'.join(config.terminology.display_metrics) or '无'}",
        "若认为某候选可以排除，在 excluded_candidates 中列出；命中决定性特征时把该特征的原文写入 decisive_feature_hit。",
    ]
    user = [
        f"已确认信息：{_format_slots(config, state)}",
        f"现场事实：{_format_observations(state.get('observations'))}",
        f"检索到的证据：{_format_evidence(cast(list[dict], state.get('evidence') or []))}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


def _candidate_names(config: ProcessConfig) -> str:
    """候选名称清单。展平所有易混淆项，保序去重后给出。"""
    names: list[str] = []
    for pair in config.confusable_items:
        for candidate in pair.candidates:
            if candidate not in names:
                names.append(candidate)
    return "、".join(names) or "无"


def _format_features(config: ProcessConfig) -> str:
    """各候选的决定性特征。只给名称不给特征，模型无法判断"命中了哪条"。"""
    parts: list[str] = []
    for pair in config.confusable_items:
        for candidate in pair.candidates:
            features = "、".join(pair.decisive_features.get(candidate, []))
            parts.append(f"{candidate}：{features}")
    return "；".join(parts) or "无"


def _format_slots(config: ProcessConfig, state: ConsultationState) -> str:
    """已确认与"答不出"的槽位。用口语标签而不是槽位键：键名对模型是噪声。"""
    summary = slot_summary(state)
    if not summary:
        return "无"
    parts: list[str] = []
    for key, value in summary.items():
        spec = config.slot_spec(key)
        parts.append(f"{spec.display_label if spec else key}={value}")
    return "；".join(parts)


def _format_observations(observations: object) -> str:
    """现场事实摘要。state 里存的是 `ObservationResult` 序列化后的字典。"""
    if not isinstance(observations, dict) or not observations:
        return "无"
    parts = [f"环境数据来源={observations.get('env_source') or '未知'}"]
    facts = observations.get("observed_facts")
    if isinstance(facts, dict) and facts:
        parts.append("现场事实：" + "；".join(f"{key}={value}" for key, value in facts.items()))
    note = observations.get("note")
    if note:
        parts.append(f"环境说明：{note}")
    return "；".join(parts)


def _format_evidence(evidence: list[dict]) -> str:
    """证据摘要：标题加内容片段。不注入条目 ID 与分值，避免模型把它们当结论要素。"""
    if not evidence:
        return "无（知识库未命中相关条目）"
    return "\n".join(
        f"- {item.get('title') or item.get('knowledge_id') or '未命名条目'}："
        f"{str(item.get('content') or '')[:_CONTENT_CAP]}"
        for item in evidence[:_MAX_EVIDENCE_ROWS]
    )


class PanelRunner:
    """三路并行会诊。仅对启用 `panel_enabled` 的流程且仅鉴别节点调用一次。

    失败行为是技术方案 6.4 的"转单链判定"：不足两路意见时不报错，返回已得到的意见，
    由调用方判定——`len(opinions) < 2` 时直接走单链（跳过面板裁定）。这个判断由 T2-26 的
    节点工厂做，面板只上报情况。
    """

    def __init__(self, gateway: ModelGateway, timeout: float = 20.0) -> None:
        self._gateway = gateway
        self._timeout = timeout

    async def collect(
        self,
        config: ProcessConfig,
        state: ConsultationState,
        ctx: AgentContext,
    ) -> list[PanelOpinion]:
        """并行收集三路意见。返回条数可能少于三，调用方据此决定是否走单链。"""
        if not config.panel_enabled:
            return []
        tasks = [asyncio.create_task(self._one(p, config, state, ctx)) for p in PERSPECTIVES]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        opinions = [r for r in results if isinstance(r, PanelOpinion)]
        if len(opinions) < 2:
            log.warning(
                "panel quorum not reached, fallback to single chain",
                extra={"trace_id": ctx.trace_id, "got": len(opinions)},
            )
        return opinions

    def already_ran(self, state: ConsultationState) -> bool:
        """是否已跑过面板。检查放在调用方而不是 `collect` 内部："是否可以重跑"属于编排决策，
        面板只提供状态查询。
        """
        return bool(cast(dict[str, Any], state).get("panel_ran"))

    async def _one(
        self,
        perspective: str,
        config: ProcessConfig,
        state: ConsultationState,
        ctx: AgentContext,
    ) -> PanelOpinion:
        """单路视角调用。超时或解析失败即抛异常，由 `collect` 的 `gather` 收走。

        不重试：三路并行本身已经是一次冗余投递，某一路失败时另有单链判定兜底，重试只会把
        整条链路拖长。
        """
        messages = build_panel_prompt(perspective, config, state, ctx)
        response = await asyncio.wait_for(
            self._gateway.chat(cast(GatewayTaskType, TaskType.PANEL), messages, PanelOpinion),
            timeout=self._timeout,
        )
        if response.error or response.parsed is None:
            raise RuntimeError(f"视角 {perspective} 未返回可用意见：{response.error or '解析失败'}")
        return PanelOpinion.model_validate(response.parsed)


__all__ = [
    "PERSPECTIVE_FOCUS",
    "PERSPECTIVES",
    "PanelAggregate",
    "PanelOpinion",
    "PanelRunner",
    "Perspective",
    "aggregate_panel",
    "all_candidates",
    "build_panel_prompt",
    "reduce_exclusions",
]
