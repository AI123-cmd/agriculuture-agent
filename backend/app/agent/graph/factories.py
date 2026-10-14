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

from collections.abc import Mapping
from typing import Any, cast

from langgraph.graph.state import StateNode
from langgraph.types import interrupt

from app.agent.agents.base import AgentContext
from app.agent.agents.diagnoser import DiagnoserAgent
from app.agent.agents.exceptions import AgentDegraded
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
from app.agent.blackboard import apply_answers, progress_of
from app.agent.budget import ConsultBudget
from app.agent.contracts import EvidenceItem, EvidenceQuery, ImageCandidate, dump_contract
from app.agent.state import ConsultationState, ConsultStage, ProcessId, SlotValue, TaskType
from app.core.config import get_settings
from app.integrations.image_reader import ModelImageReader
from app.integrations.storage import get_storage
from app.llm import get_gateway
from app.observability import traced_node
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

# 工厂产出的节点函数：只读 state（含本轮入参注入），返回本轮改动的键。
# 用 LangGraph 的节点协议标注而非裸 `Callable` 别名：`StateGraph.add_node` 只接受节点
# 协议，裸 `Callable[...] -> Awaitable[dict]` 会被 mypy 判为不匹配（见 main.py 同款说明）。
NodeFn = StateNode[ConsultationState, None]


def get_config(process_id: ProcessId) -> ProcessConfig:
    """读取流程配置（走加载器的进程内缓存）。未注册/损坏一律抛 `ConfigLoadError`。

    刻意不返回 `None`：配置缺失时静默兜底会让「流程不可用」变成没人知道的线上事实，
    正确处置是在加载阶段终结，由路由层把该流程指向 `handoff`。
    """
    return get_process_config(process_id)


def _traced(kind: str, config: ProcessConfig, node: NodeFn) -> NodeFn:
    """给工厂产出的节点套统一追踪（T6-06）。

    集中在工厂里包一次，七个节点与新增流程都自动获得 span，避免逐个节点手写——
    属性口径一旦分散，后端的失败率与成本分组就会因写法差异而漏数。

    `name` 保持 `{kind}_{流程}`（工厂靠它区分流程，`test_factories` 会断言），
    `span_name` 用图上的注册名（`observe`/`interview`/…），使"子 span 名为节点名"成立。
    """
    return cast(NodeFn, traced_node(f"{kind}_{config.process_id.value}", node, span_name=kind))


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


def _image_reader() -> ImageReader:
    """视觉识别端口：未配置真实端点时回落空实现，保证 mock 模式可用。

    与 `_EmptyEnvReader` 的处理方式一致：没端点不是错误，只是"这轮没有图片事实"。真实
    端点的判定与网关装配同源（`USE_MOCK_LLM` / 端点地址），避免出现"网关打向真实模型、
    图片识别却仍走空实现"这类不一致。
    """
    settings = get_settings()
    if settings.USE_MOCK_LLM or not (settings.FARM_LLM_API_URL or settings.OPENAI_COMPAT_BASE_URL):
        return _EMPTY_IMAGE
    return ModelImageReader(get_gateway(), get_storage())


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
    observer = FieldObserverAgent(get_gateway(), _EmptyEnvReader(), _image_reader())

    async def observe(state: ConsultationState) -> dict:
        result = await observer.run(state, _context_for(state, config, TaskType.OBSERVE))
        return {
            "observations": dump_contract(result),
            "stage": ConsultStage.OBSERVE,
            # 照片回执：让农户立刻知道"AI 看过照片了"。无候选时为空串，不污染既有回复。
            "reply": _photo_receipt(result.image_candidates),
        }

    return _traced("observe", config, observe)


def _photo_receipt(candidates: list[ImageCandidate]) -> str:
    """拍照回执：把识别结果说成一句人话。没候选时返回空串（不污染既有回复）。

    只说"像是"，不报置信度数字、不下诊断结论——图片识别结果不构成证据（见
    `FieldObserverAgent.image_to_slots` 的说明），分数只进日志与槽位。
    """
    if not candidates:
        return ""
    seen = "、".join(c.label for c in candidates[:3])
    return f"我看过您发的照片了，照片里像是：{seen}。我再问您几个问题，好判断得更准。"


def make_interview_node(config: ProcessConfig) -> NodeFn:
    """问诊节点工厂：必问清单、候选池排序、术语口语化映射、每轮问题上限均由配置决定。

    本节点是「等农户回答」这一类中断的落点（T2-32）：生成本轮问题后 `interrupt` 暂停，
    检查点保存完整状态，下个 HTTP 请求带答案恢复。恢复时节点从头执行、重新生成同一批
    问题（候选池与问法都是确定性生成的，重复执行不产生不同问题），`interrupt` 的返回值
    即农户答案，随后套用答案并记一轮问诊。

    关键顺序约束：`interrupt` 必须在**写入新状态之前**调用，这样恢复时 `state` 仍是中断
    那一刻的快照；若先写答案再中断，恢复会读到已被修改的 state，答案被套用两次。
    """
    interviewer = InterviewerAgent(get_gateway(), ConsultBudget())
    # 追问卡片里的"拍张照片说明情况"由农户拍照作答：识别端口与现场采集同源，
    # 复用同一个 FieldObserverAgent 的识别与候选域过滤，两条路径对"哪些标签算数"给出同一答案。
    observer = FieldObserverAgent(get_gateway(), _EmptyEnvReader(), _image_reader())

    async def interview(state: ConsultationState) -> dict:
        try:
            batch = await interviewer.run(state, _context_for(state, config, TaskType.INTERVIEW))
        except AgentDegraded:
            # 无题可问：必问槽位已全部解决（或只剩自动来源可覆盖的项），问诊已无事可做。
            # 降级出口抛 `AgentDegraded` 的本意就是"应由主图推进到下一环节"，若任由它冒泡，
            # 农户收到的是 500 而不是一条正常回复。此处不中断、直接推进；同时记一轮问诊，
            # 使 `route_after_interview` 的轮次上限继续生效，异常路径不绕开预算。
            budget = ConsultBudget(**state.get("budget", {}))
            budget.note_interview_round()
            return {
                "slots": state.get("slots") or {},
                "budget": budget.to_state(),
                "pending_questions": [],
                "stage": ConsultStage.INTERVIEW,
                "panel_needed": config.panel_enabled,
            }
        payload = {
            "questions": [q.model_dump(mode="json") for q in batch.questions],
            "stage_hint": batch.stage_hint,
            "progress": progress_of(state),
        }
        # 中断点：此处暂停，返回值即恢复时传入的答案（形态见 T2-33 的 ChatRequest.answers）。
        answers = interrupt(payload)

        budget = ConsultBudget(**state.get("budget", {}))
        budget.note_interview_round()
        slots = apply_answers(state.get("slots") or {}, answers)
        # 追问卡片里的拍照作答：农户用照片回答"能不能拍张照片说明情况"这类问题时，
        # 照片本身就是答案。识别结果写进该题对应槽位（来源标注为 image），并回一句
        # 农户看得懂的识别说明。识别不到候选时槽位仍记为已答，避免同一题被反复追问。
        reply, slots = await _absorb_photo_answer(state, config, observer, payload, answers, slots)
        return {
            "slots": slots,
            "budget": budget.to_state(),
            "pending_questions": payload["questions"],
            "stage_hint": batch.stage_hint,
            # 会诊必要性由代码按流程配置置位：路由只读这个布尔量，不对自由文本做关键词匹配。
            "panel_needed": config.panel_enabled,
            "stage": ConsultStage.INTERVIEW,
            "reply": reply,
        }

    interview.__name__ = f"interview_{config.process_id.value}"
    return _traced("interview", config, interview)


async def _absorb_photo_answer(
    state: ConsultationState,
    config: ProcessConfig,
    observer: FieldObserverAgent,
    payload: dict[str, Any],
    answers: object,
    slots: dict[str, SlotValue],
) -> tuple[str, dict[str, SlotValue]]:
    """把恢复值里的照片当作本轮 photo 类型问题的答案：识别后写入槽位并生成回执文案。

    照片只有在"本轮确实问了 photo 类型的问题"时才作为答案处理——这既避免把普通的
    中途发图误当成作答，也让"写哪个槽位"有明确出处（问题的 `slot_key`），不依赖
    前端额外声明。识别失败（无候选）时槽位仍记为已答，否则问诊会拿同一题反复追问。
    """
    resume: Mapping[str, Any] = answers if isinstance(answers, Mapping) else {}
    image_url = resume.get("image_url")
    slot_key = next(
        (
            row.get("slot_key") or row.get("slotKey")
            for row in payload.get("questions") or []
            if row.get("answer_type") == "photo"
        ),
        None,
    )
    if not image_url or not isinstance(slot_key, str) or not slot_key:
        return "", slots

    candidates = await observer.recognize_images([str(image_url)], config)
    label = candidates[0].label if candidates else ""
    merged = dict(slots)
    merged[slot_key] = SlotValue(
        key=slot_key,
        value=label or "农户已拍照说明",
        status="filled",
        source="image" if label else "user_answer",
        confidence=candidates[0].score if candidates else None,
    )
    return _photo_receipt(candidates), merged


def make_verify_node(config: ProcessConfig) -> NodeFn:
    """证据核查节点工厂：检索的元数据过滤条件（process_id、region_scope、status=approved）
    由 config 与注入的检索端口决定。

    空库时的模型兜底依据（`state["model_basis"]`）由检索智能体原地写入 state，而 LangGraph
    **会静默丢弃**节点返回的、未在 `ConsultationState` 中声明的键，因此这里必须把该键一并
    作为返回值提交——与 judge 节点回传 `confidence_score` 是同一处约束。未开启兜底或未触发
    兜底时该键不存在，返回 `None` 让下游读到"没有兜底依据"。
    """
    retriever = RetrieverAgent(get_gateway(), _EmptyKnowledge(), _NoRegionMatcher())

    async def verify(state: ConsultationState) -> dict:
        evidence_set = await retriever.run(state, _context_for(state, config, TaskType.RETRIEVE))
        return {
            "evidence": [item.model_dump(mode="json") for item in evidence_set.items],
            "model_basis": cast(dict[str, Any], state).get("model_basis"),
            "stage": ConsultStage.VERIFY,
        }

    verify.__name__ = f"verify_{config.process_id.value}"
    return _traced("verify", config, verify)


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
        # 记一次判定回环：`route_after_judge` 用 `budget["judge_loops"]` 决定"再补问一次"还是
        # "转人工"，而这个计数此前无人递增（`ConsultBudget.note_judge_loop` 仅测试调用过），
        # 导致上限形同虚设、置信一直不足时会无限回环。判据与路由保持一致：只有在**即将回环**
        # 时（不确定、待补信息、且未超边界）才递增，避免正常的开方路径白耗预算。
        if result.confidence_level == "uncertain" and result.needs_more_info and not result.out_of_scope:
            budget = ConsultBudget(**state.get("budget", {}))
            budget.note_judge_loop()
            out["budget"] = budget.to_state()
        return out

    judge.__name__ = f"judge_{config.process_id.value}"
    return _traced("judge", config, judge)


def make_prescribe_node(config: ProcessConfig) -> NodeFn:
    """处方节点工厂：输出模板、安全间隔期口径、`calendar_source` 是否为空、禁用输出均由配置
    与注入的检索端口决定。"""
    prescriber = PrescriberAgent(get_gateway(), _EmptyKnowledge(), _NoRegionMatcher())

    async def prescribe(state: ConsultationState) -> dict:
        result = await prescriber.run(state, _context_for(state, config, TaskType.PRESCRIBE))
        return {"prescription": dump_contract(result), "stage": ConsultStage.PRESCRIBE}

    prescribe.__name__ = f"prescribe_{config.process_id.value}"
    return _traced("prescribe", config, prescribe)


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
    return _traced("panel", config, panel)


def make_handoff_node(config: ProcessConfig) -> NodeFn:
    """转人工节点工厂：四种情形（超边界/安全拦截/未收敛/法定报告）各自的输出模板由配置决定。

    直接复用 `build_handoff_node(config)`：转人工的判定与渲染是通用实现，形态与报告守卫的
    `build_report_guard_node(config)` 保持一致，子图与主图共用同一节点函数。
    """
    return _traced("handoff", config, build_handoff_node(config))


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
