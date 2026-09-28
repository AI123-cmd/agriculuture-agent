"""通用节点工厂：config 决定行为，节点实现由各智能体提供（T2-26）。

七条流程共用的五个节点（observe / interview / verify / judge / prescribe）以及 panel、
handoff 两个结构节点，全部以「工厂闭包 + 流程配置」的形式产出：子图文件只负责把配置注入
通用节点，差异全部来自 `ProcessConfig`。这是「流程配置是数据而非代码」的落地点——新增第
八条流程时改动量被限定为一份 YAML + 约十五行子图文件 + 注册表一行，不触碰任何智能体实现，
也不触碰闸门规则。

工厂本身不实现重试：契约校验、有限次重试与降级由基类（`BaseAgent`）统一执行，工厂只负责
构造 `AgentContext` 并调用 `agent.run(state, ctx)`。降级策略（守卫类 fail-closed、其余降级
仍服务）由各智能体与基类决定。

**与旧接口的对接说明**（本项落地时按新文档纠正了若干历史签名，旧形态全部废弃）：

1. `get_config` 只有一个入参：真实加载器 `load_process_config(process_id)` 是单参且抛
   `ConfigLoadError`，没有 `version` 形参；「未注册流程返回 None」的旧约定废弃——配置缺失
   必须抛异常，否则下游的 `None` 检查会退化成「反正不会为 None」的假设。缓存复用加载器
   自带的进程内缓存（`get_process_config`），不再叠加一层 `lru_cache`，避免热加载（T2-05）
   刷新加载器缓存后工厂缓存仍旧命中。
2. 节点函数签名统一为 `async def node(state) -> dict`，与 `build_report_guard_node` /
   `build_handoff_node` 的产出一致（不带 `runtime` 形参：LangGraph 仅在形参名为 `config`
   时注入 `RunnableConfig`，本层节点不需要它）。
3. `RetrieverAgent.run` 返回 `EvidenceSet`（不是列表），证据条目从 `EvidenceSet.items` 取。
4. 没有 `build_panel_graph`：并行会诊的真实形态是 `PanelRunner.collect` + 纯规则
   `aggregate_panel`，因此 `make_panel_node` 返回一个普通节点函数（未启用时返回 `None`，
   子图据此不注册该节点）。
5. `DiagnoserAgent.post_validate` 会把内部置信度分数原地写进 `state["confidence_score"]`；
   而 LangGraph **会静默丢弃**节点返回的、未在 `ConsultationState` 中声明的键，所以 judge
   节点必须把该键一并作为返回值提交，否则分数不会被持久化。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from app.agent.agents.base import AgentContext
from app.agent.agents.diagnoser import DiagnoserAgent
from app.agent.agents.field_observer import (
    EnvReader,
    FieldObserverAgent,
    ImageReader,
    RawReading,
)
from app.agent.agents.handoff import build_handoff_node
from app.agent.agents.interviewer import InterviewerAgent
from app.agent.agents.panel import PanelRunner, aggregate_panel
from app.agent.agents.prescriber import PrescriberAgent
from app.agent.agents.retriever import KnowledgeRetriever, RegionMatcher, RetrieverAgent
from app.agent.budget import ConsultBudget
from app.agent.contracts import EvidenceItem, EvidenceQuery, ImageCandidate, dump_contract
from app.agent.state import ConsultStage, ConsultationState, ProcessId, TaskType
from app.llm import get_gateway
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

# 工厂产出的节点函数：只读 state（含本轮入参注入），返回本轮改动的键。
NodeFn = Callable[[ConsultationState], Awaitable[dict]]


def get_config(process_id: ProcessId) -> ProcessConfig:
    """读取流程配置（走加载器的进程内缓存）。未注册/损坏一律抛 `ConfigLoadError`。

    刻意不返回 `None`：配置缺失时静默兜底会让「流程不可用」变成没人知道的线上事实，
    正确处置是在加载阶段终结，由路由层把该流程指向 `handoff`。
    """
    return get_process_config(process_id)


# ---- 外部端口占位实现 ----
# 环境读数、图片识别、知识检索、地区匹配四个端口的生产实现分属 T5-08 / T2-49 / T2-50，
# 本阶段由占位实现注入：返回空结果（不是编造数据），使全链路可在 mock 模式下跑通。
# 形态与报告守卫的 `_EmptyDirectory` 占位一致。


class _EmptyEnvReader:
    """未接入环境设备时的占位：无读数。"""

    async def read(self, plot_id: str | None) -> list[RawReading]:
        return []


class _EmptyImageReader:
    """未接入图片识别时的占位：无候选。"""

    async def recognize(self, image_urls: list[str], *, allowed_labels: set[str]) -> list[ImageCandidate]:
        return []


class _EmptyKnowledge:
    """未接入知识库时的占位：无命中。"""

    async def search(self, query: EvidenceQuery, region_chain: list[str]) -> list[EvidenceItem]:
        return []


class _NoRegionMatcher:
    """未接入地区回退链时的占位：空链，检索直接判定未命中。"""

    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        return []


# 标注一次，让占位实现与协议的结构一致性由 mypy 在这里核对（而非等到运行时）。
_EMPTY_ENV: EnvReader = _EmptyEnvReader()
_EMPTY_IMAGE: ImageReader = _EmptyImageReader()
_EMPTY_KNOWLEDGE: KnowledgeRetriever = _EmptyKnowledge()
_NO_REGION: RegionMatcher = _NoRegionMatcher()


def _remaining_tokens(state: ConsultationState) -> int | None:
    """从预算快照推导剩余 token；预算缺失时返回 `None`（表示不限制重试）。"""
    budget = state.get("budget")
    if not isinstance(budget, dict):
        return None
    max_tokens = budget.get("max_tokens_per_consult")
    if not isinstance(max_tokens, int):
        return None
    used = budget.get("tokens_used")
    return max_tokens - (used if isinstance(used, int) else 0)


def _context_for(state: ConsultationState, config: ProcessConfig, task_type: TaskType) -> AgentContext:
    """构造一次智能体调用的环境依赖。全部来自 state 与流程配置，不查库、不读外部资源。"""
    return AgentContext(
        process_config=config,
        config_version=state.get("config_version") or config.version,
        task_type=task_type,
        trace_id=state.get("trace_id") or "",
        region_code=state.get("region_code"),
        budget_remaining_tokens=_remaining_tokens(state),
    )


def make_observe_node(config: ProcessConfig) -> NodeFn:
    """现场采集节点工厂：是否需要图片、环境事实三级回退口径均由 config 与注入端口决定。"""
    observer = FieldObserverAgent(get_gateway(), _EmptyEnvReader(), _EmptyImageReader())

    async def observe(state: ConsultationState) -> dict:
        result = await observer.run(state, _context_for(state, config, TaskType.OBSERVE))
        return {"observations": dump_contract(result), "stage": ConsultStage.OBSERVE}

    observe.__name__ = f"observe_{config.process_id.value}"
    return observe


def make_interview_node(config: ProcessConfig) -> NodeFn:
    """问诊节点工厂：必问清单、候选池排序、术语口语化映射、每轮问题上限均由配置决定。"""
    interviewer = InterviewerAgent(get_gateway(), ConsultBudget())

    async def interview(state: ConsultationState) -> dict:
        batch = await interviewer.run(state, _context_for(state, config, TaskType.INTERVIEW))
        return {
            "pending_questions": [q.model_dump(mode="json") for q in batch.questions],
            "stage_hint": batch.stage_hint,
            # 会诊必要性由代码按流程配置置位：路由只读这个布尔量，不对自由文本做关键词匹配。
            "panel_needed": config.panel_enabled,
            "stage": ConsultStage.INTERVIEW,
        }

    interview.__name__ = f"interview_{config.process_id.value}"
    return interview


def make_verify_node(config: ProcessConfig) -> NodeFn:
    """证据核查节点工厂：检索的元数据过滤条件（process_id、region_scope、status=approved）
    由 config 与注入的检索端口决定。"""
    retriever = RetrieverAgent(get_gateway(), _EmptyKnowledge(), _NoRegionMatcher())

    async def verify(state: ConsultationState) -> dict:
        evidence_set = await retriever.run(state, _context_for(state, config, TaskType.RETRIEVE))
        return {
            "evidence": [item.model_dump(mode="json") for item in evidence_set.items],
            "stage": ConsultStage.VERIFY,
        }

    verify.__name__ = f"verify_{config.process_id.value}"
    return verify


def make_judge_node(config: ProcessConfig) -> NodeFn:
    """判定节点工厂：易混淆项与决定性特征、`excluded` 强制非空、术语档均由配置决定。"""
    diagnoser = DiagnoserAgent(get_gateway(), ConsultBudget())

    async def judge(state: ConsultationState) -> dict:
        result = await diagnoser.run(state, _context_for(state, config, TaskType.DIAGNOSE))
        out: dict = {"diagnosis": dump_contract(result), "stage": ConsultStage.JUDGE}
        # 判定智能体把内部置信度分数原地写进 state；LangGraph 不持久化未作为返回值提交的键，
        # 这里显式回传，供后续路由/审计读取（分数不得进入任何展示文案）。
        score = cast(dict[str, Any], state).get("confidence_score")
        if isinstance(score, (int, float)):
            out["confidence_score"] = float(score)
        return out

    judge.__name__ = f"judge_{config.process_id.value}"
    return judge


def make_prescribe_node(config: ProcessConfig) -> NodeFn:
    """处方节点工厂：输出模板、安全间隔期口径、`calendar_source` 是否为空、禁用输出均由配置
    与注入的检索端口决定。"""
    prescriber = PrescriberAgent(get_gateway(), _EmptyKnowledge(), _NoRegionMatcher())

    async def prescribe(state: ConsultationState) -> dict:
        result = await prescriber.run(state, _context_for(state, config, TaskType.PRESCRIBE))
        return {"prescription": dump_contract(result), "stage": ConsultStage.PRESCRIBE}

    prescribe.__name__ = f"prescribe_{config.process_id.value}"
    return prescribe


def make_panel_node(config: ProcessConfig) -> NodeFn | None:
    """并行会诊节点工厂：仅 `panel_enabled` 的流程返回节点，否则返回 `None`（子图不注册）。

    三路视角并行收集后由纯规则聚合器裁定；聚合器为纯函数、无模型消耗，裁定结果写进
    `panel_aggregate`，并用 `panel_ran` 标记已执行、`panel_force_uncertain` 供判定智能体
    在未收敛时强制输出不确定。
    """
    if not config.panel_enabled:
        return None
    runner = PanelRunner(get_gateway())

    async def panel(state: ConsultationState) -> dict:
        if runner.already_ran(state):
            return {}
        opinions = await runner.collect(config, state, _context_for(state, config, TaskType.PANEL))
        aggregate = aggregate_panel(opinions, config)
        return {
            "panel_aggregate": dump_contract(aggregate),
            "panel_ran": True,
            "panel_force_uncertain": aggregate.force_uncertain,
        }

    panel.__name__ = f"panel_{config.process_id.value}"
    return panel


def make_handoff_node(config: ProcessConfig) -> NodeFn:
    """转人工节点工厂：四种情形（超边界/安全拦截/未收敛/法定报告）各自的输出模板由配置决定。

    直接复用 `build_handoff_node(config)`：转人工的判定与渲染是通用实现，形态与报告守卫的
    `build_report_guard_node(config)` 保持一致，子图与主图共用同一节点函数。
    """
    return build_handoff_node(config)


__all__ = [
    "NodeFn",
    "get_config",
    "make_handoff_node",
    "make_interview_node",
    "make_judge_node",
    "make_observe_node",
    "make_panel_node",
    "make_prescribe_node",
    "make_verify_node",
]
