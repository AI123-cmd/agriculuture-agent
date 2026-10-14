"""证据核查智能体：把槽位与流程改写为检索请求，取回已核准的知识条目（T2-18）。

它是"查依据"这一步的实现者，也是"模板出事实、模型只改写"原则的上游：处方里的药剂与剂量
事实都来自这里检索到的已核准条目，**没有任何一条事实由模型生成**。因此本智能体与其余六个
不同：主流程完全由代码驱动（改关键词、建请求、逐级回退检索、硬过滤），模型只承担一件事
——把农户的口语表述改写成植保检索用词。这也是改动清单里提示词只有"查询改写"一项的原因。

三类失败都在这里显式处置，而不是让下游自己猜：

- **知识库未命中**：`hit=False` 加 `unmatched_reason`。空集是合法输出，判定据此降低确定性
  表述并给观察建议（T2-19），处方据此要求联系农技站（T2-21）。编造证据才是错的。
- **跨流程条目**：`enforce_process_scope` 做二次硬过滤。契约层（T2-10）已有一条同类自检，
  两道防线并存是因为检索实现会被反复改写（换向量库、换重排模型），而"茶流程用叶发病率、
  水稻用病丛率"这条不变量必须在每次改写后依然成立。
- **检索不可用**：`degrade` 返回空集并标注原因，不重试。检索失败通常是知识库不可用这类
  不会立刻恢复的原因，重试三次只会把问诊延迟拉长十几秒。

`KnowledgeRetriever` 与 `RegionMatcher` 是协议而非具体类：L2 规则要求 `app/agent/` 不得直接
导入 ORM，且智能体要能被喂假数据单测。两者的实现分别在 T2-49 与 T2-50。
"""

import asyncio
import json
import re
from typing import Any, Protocol, cast

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.blackboard import slot_summary
from app.agent.contracts import EvidenceItem, EvidenceQuery, EvidenceSet, ModelBasis
from app.agent.contracts.evidence import EvidenceKindValue, RegionLevelActual
from app.agent.prompts.model_fallback import build_model_fallback_prompt
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.core.config import get_settings
from app.llm import ModelGateway
from app.llm.enums import TaskType as GatewayTaskType
from app.observability import log, metrics
from app.process_config.models import ProcessConfig

# 未命中时的说明。它必须能区分"知识库确实没有"与"检索故障"——后者由 `degrade` 给出带原因
# 的文案，两者在界面上分别是"暂时查不到适合您当地的条目"与"查询服务暂不可用"。
_UNMATCHED = "知识库未命中该流程与地区的相关条目"

# 兜底触发次数的指标名。只统计"成功产出可用兜底依据"的次数：失败与超时是可选增强的
# 正常损耗，混进同一个计数会让"兜底到底帮上忙了没有"无法回答。
_FALLBACK_METRIC = "verify_fallback_total"

# 启用并行会诊的流程要更大的证据空间（三条视角各自需要材料），其余流程 8 条足够。
_TOP_K_PANEL = 10
_TOP_K_DEFAULT = 8

# 改写结果的分隔符。模型被要求用顿号，但实际会混用逗号、分号与空白，全部当分隔符处理。
_SPLIT = re.compile(r"[、,，;；\s]+")


class KnowledgeRetriever(Protocol):
    """知识库混合检索的只读接口。T2-49 提供实现，本阶段由假实现注入。

    `region_chain` 是整条回退链（从区县到全国），供检索实现做相关性加权——回退的**判定**
    循环在本模块（某级非空即停止），因为"哪一级够用"属地区匹配职责，而 T2-49 的 `search`
    保持"一个查询一个地区"的单一语义便于单测。
    """

    async def search(self, query: EvidenceQuery, region_chain: list[str]) -> list[EvidenceItem]:
        """检索单级地区的知识条目。无命中返回空列表，不可用抛异常。"""


class RegionMatcher(Protocol):
    """地区回退链的构造接口。T2-50 提供实现。"""

    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        """返回 (行政级别, 编码) 的有序回退链，从区县到全国。"""


def build_rewrite_prompt(keywords: list[str], config: ProcessConfig) -> list[dict]:
    """查询改写提示词。

    注入流程的 `internal_terms` 而不是泛泛地要求"专业化"：茶流程把"叶子起油"改写成"茶饼病"
    靠的是这份术语表，术语表又来自流程配置，因此改写口径随流程变化而无需改代码。
    """
    system = [
        "你是农业检索词改写器。把农户口语改写成植保检索用词。",
        f"本流程的标准术语：{json.dumps(config.terminology.internal_terms, ensure_ascii=False)}",
        "只输出改写后的词，用顿号分隔。不要解释。",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "、".join(keywords)},
    ]


def _dedupe(words: list[str]) -> list[str]:
    """保序去重。不排序：保序让高优先来源（用户回答）排在前面，检索侧做权重衰减时能用上。"""
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        stripped = word.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            out.append(stripped)
    return out


def _parse_rewrite(text: str) -> list[str]:
    """把改写的自由文本切成检索用词。

    只保留纯文字数字的词：改写结果里混进的括号说明（"（以下略）"）或标点片段不是检索用词，
    留着只会稀释关键词的区分度。
    """
    return [token for token in (part.strip() for part in _SPLIT.split(text)) if token and token.isalnum()]


class RetrieverAgent(BaseAgent[EvidenceSet]):
    """证据核查：改写关键词、按地区回退检索、硬过滤跨流程条目、组装证据集。"""

    name = "retriever"
    output_schema = EvidenceSet
    task_type = TaskType.RETRIEVE
    failure_mode = FailureMode.DEGRADE  # 返回空集并标注未命中
    # 检索不需要重试语义：失败的处置是返回空集让判定降级，而不是重试三次再失败。注意基类
    # 的 `max_attempts` 语义是"首次之外的额外次数"，而本智能体的主流程不走基类的重试入口
    # （见 `run`），该声明只用于表达"不重试"的意图。
    max_attempts = 1

    def __init__(
        self,
        gateway: ModelGateway,
        knowledge: KnowledgeRetriever,
        region_matcher: RegionMatcher,
    ) -> None:
        super().__init__(gateway)
        self._knowledge = knowledge
        self._region = region_matcher

    async def run(self, state: ConsultationState, ctx: AgentContext) -> EvidenceSet:
        """主流程：改词、建请求、检索、过滤、组装，失败走降级。

        不走基类的"调模型出契约"入口：本智能体的产出全部是检索事实，没有一项由模型生成，
        让模型先编一版证据再由代码整份覆盖只会多一次调用与一份可能被误用的中间结果。降级
        与指标仍复用基类的失败分派（`_on_failure`），行为与其余智能体一致。

        空集时在既有产出之外**可选地**补一次模型兜底（见 `_maybe_model_fallback`）：它只在
        "库里确实没有"时报效，"检索故障"已在 `_on_failure` 的降级出口返回，不会走到这里。
        """
        try:
            draft = await self.assemble(state, ctx)
            result = await self.post_validate(draft, state, ctx)
        except Exception as exc:
            # 宽泛捕获是刻意的：调用方（图节点）不该因为检索故障而整条链路崩掉，降级出口
            # 就是为这种情况准备的。
            return await self._on_failure(state, ctx, exc)
        await self._maybe_model_fallback(state, ctx, result)
        return result

    async def assemble(self, state: ConsultationState, ctx: AgentContext) -> EvidenceSet:
        """组装证据集：关键词改写 → 建请求 → 逐级检索 → 跨流程硬过滤。"""
        query = self.build_query(state, ctx)
        rewritten = await self.rewrite_keywords(query.keywords, ctx.process_config)
        if rewritten != query.keywords:
            query = query.model_copy(update={"keywords": rewritten})
        items, level, attempted = await self.run_search(query, ctx)
        items = self.enforce_process_scope(items, ctx.process_config.process_id)
        return EvidenceSet(
            query=query,
            items=items,
            hit=bool(items),
            region_level_actual=cast(RegionLevelActual, level),
            reranked=False,
            fallback_chain=attempted,
            unmatched_reason=None if items else _UNMATCHED,
        )

    # ---- 检索请求的构造 ----

    def build_query(self, state: ConsultationState, ctx: AgentContext) -> EvidenceQuery:
        """把槽位与流程信息压成检索请求。`process_id` 为硬过滤。"""
        config = ctx.process_config
        return EvidenceQuery(
            process_id=config.process_id,
            keywords=self._extract_keywords(state, config),
            region_code=ctx.region_code,
            slot_snapshot=slot_summary(state),
            kind_filter=cast(list[EvidenceKindValue], self.kind_filter_for(config)),
            top_k=self._top_k_for(config),
        )

    def _top_k_for(self, config: ProcessConfig) -> int:
        """启用并行会诊的流程要更大的候选集，三条视角各自需要证据空间。"""
        return _TOP_K_PANEL if config.panel_enabled else _TOP_K_DEFAULT

    def kind_filter_for(self, config: ProcessConfig) -> list[str]:
        """条目类型的过滤集合。油菜是唯一的例外，见下。

        技术方案 9.5 要求把油菜流程的 L3 处置知识检索开关设为 `disabled`——油菜的药剂推荐
        必须由红线 `RAPE-01` 拦截，因此在候选集层面就不召回药剂条目。这一层比红线更早生效，
        红线作为第二道防线保留：两道防线并存是为了防止知识库中误入油菜药剂条目。
        """
        if config.process_id is ProcessId.RAPESEED:
            return ["pest", "disease", "cultivation", "threshold"]  # 排除 pesticide
        return []

    def _extract_keywords(self, state: ConsultationState, config: ProcessConfig) -> list[str]:
        """关键词来源：已填槽位值、流程指标口径、易混淆项候选、现场事实。

        `display_metrics` 必须进关键词，它保证"茶流程用叶发病率检索而不是株发病率"；易混淆项
        候选保证涉及鉴别的查询能召回判据类条目。两项都来自流程配置，不硬编码。
        """
        words: list[str] = []
        for slot in (state.get("slots") or {}).values():
            if slot.status == "filled" and slot.value:
                words.append(slot.value)
        words.extend(config.terminology.display_metrics)
        for pair in config.confusable_items:
            words.extend(pair.candidates)
        observations = state.get("observations") or {}
        for value in (observations.get("observed_facts") or {}).values():
            if value:
                words.append(value)
        return _dedupe(words)

    # ---- 查询改写的模型使用 ----

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """基类契约要求的提示词构造。本智能体唯一的模型调用是查询改写，故这里就是它。"""
        return build_rewrite_prompt(self._extract_keywords(state, ctx.process_config), ctx.process_config)

    async def rewrite_keywords(self, keywords: list[str], config: ProcessConfig) -> list[str]:
        """用模型把口语化表述改写成检索用词。失败则原样返回。

        改写是**增量**的：模型产出的词追加在原词之后，而不是替换。这样即使改写偏离，原始
        表述仍在请求里，检索不会因为一次失败的改写而完全走偏。改写失败不触发降级，因为它
        是一个可选的增强步骤。
        """
        if not keywords:
            return []
        try:
            rewritten = await self._rewrite_once(keywords, config)
        except Exception as exc:
            log.warning("keyword rewrite failed, use raw", reason=str(exc))
            return keywords
        return _dedupe([*keywords, *rewritten])

    async def _rewrite_once(self, keywords: list[str], config: ProcessConfig) -> list[str]:
        """一次改写调用。响应带错误或内容为空时视为没有可用的改写结果。

        不用结构化输出：这个任务只要一串词，套一层 Schema 只会让模型为凑字段而多写内容；
        解析在 `_parse_rewrite` 里做，且结果只作为增量追加，出错的代价很低。
        """
        messages = build_rewrite_prompt(keywords, config)
        response = await self._require_gateway().chat(cast(GatewayTaskType, self.task_type), messages)
        if response.error or not response.content:
            return []
        return _parse_rewrite(response.content)

    # ---- 检索与地区回退 ----

    async def run_search(self, query: EvidenceQuery, ctx: AgentContext) -> tuple[list[EvidenceItem], str, list[str]]:
        """按地区回退链逐级检索，命中即止。返回证据、实际命中级别与尝试链。

        回退链由 `RegionMatcher` 构造，顺序为区县、市、省、全国。某一级召回非空即停止：县级
        命中说明农户所在地有专属条目，继续往下取只会引入更泛化的条目。`fallback_chain`
        保留全量尝试路径而非只留终点，因为线上排查最想知道的是"哪一级开始命中"。
        """
        chain = self._region.build_chain(query.region_code)
        attempted: list[str] = []
        for level, code in chain:
            attempted.append(f"{level}:{code}")
            scoped = query.model_copy(update={"region_code": code})
            items = await self._knowledge.search(scoped, region_chain=[c for _, c in chain])
            if items:
                return items, level, attempted
        return [], "none", attempted

    def enforce_process_scope(self, items: list[EvidenceItem], process_id: ProcessId) -> list[EvidenceItem]:
        """二次硬过滤：丢弃跨流程条目。检索侧过滤之外的自检。

        通用流程的条目允许混入任意流程，因为它本身就是兜底知识（无专属阈值、无药剂条目）。
        其他流程的条目严格限定在自身。这层过滤与 T2-10 契约里的 `_all_items_same_process`
        校验器形成双保险：契约层拦结构性错误，这里拦检索侧的疏漏。
        """
        kept: list[EvidenceItem] = []
        for item in items:
            if item.process_id in (process_id.value, ProcessId.GENERAL.value):
                kept.append(item)
            else:
                log.warning(
                    "cross-process evidence dropped",
                    knowledge_id=item.knowledge_id,
                    process_id=item.process_id,
                )
        return kept

    # ---- 组装与降级 ----

    async def post_validate(self, result: EvidenceSet, state: ConsultationState, ctx: AgentContext) -> EvidenceSet:
        """补齐未命中原因、按 `knowledge_id` 去重、按分值截断到 `top_k`。

        去重必须发生在截断之前：混合检索（BM25 加向量）的不同通路会召回同一条目，先去重
        才能让 `top_k` 个位置都留给不同的条目。保留分值更高者，因为两条通路给出的分数反映
        的是同一份相关性，取高的一次更接近重排序的结论。升序遍历再写字典，后写覆盖前写，
        留下的正是更高分的那一条。
        """
        unique: dict[str, EvidenceItem] = {}
        for item in sorted(result.items, key=lambda i: i.score):
            unique[item.knowledge_id] = item
        items = sorted(unique.values(), key=lambda i: i.score, reverse=True)[: self._top_k_for(ctx.process_config)]
        updates: dict[str, Any] = {"items": items, "hit": bool(items)}
        if not items and not result.unmatched_reason:
            updates["unmatched_reason"] = _UNMATCHED
        return result.model_copy(update=updates)

    # ---- 知识库为空时的模型兜底 ----

    async def _maybe_model_fallback(
        self, state: ConsultationState, ctx: AgentContext, result: EvidenceSet
    ) -> None:
        """知识库空集时请模型补充判据与处置方向，写进 `state["model_basis"]`。

        这是②-B 方案（零闸门改动）的落点，且**默认关闭**（`ALLOW_MODEL_FALLBACK`）。三条
        边界决定了它的形态：

        - **只产文本，不产事实**：产出经 `ModelBasis` 契约过滤，含剂量数字的条目被丢弃，
          药剂仅以名称出现。下游处方把药名织进非药剂处置建议的文本，`pesticide_items`
          恒空，因此闸门对药剂的全部校验（白名单/合理区间/登记作物/间隔期）依旧无事可做。
        - **永不写入知识库**：它是本次问诊的临时输入，不落库、不进 `evidence`、不参与
          `facts_provenance` 的溯源登记。
        - **失败静默回落**：超时、解析失败、网关不可用一律只看日志，不抛异常、不改
          `EvidenceSet`。兜底是可选增强，为它让整条问诊失败是本末倒置。

        触发条件是"库里确实空"而不是"检索故障"：后者已在 `run` 的 except 分支返回降级集，
        本方法只被正常路径调用。
        """
        if not self._fallback_enabled():
            return
        if ctx.process_config.process_id is ProcessId.RAPESEED:
            # 油菜流程不兜底：红线 RAPE-01 是产品硬要求（"调研补齐前禁止输出药剂推荐类
            # 内容"），而兜底会产出药剂名称——即便只给名称也是推荐类内容。这是与检索侧
            # `kind_filter_for` 排除 `pesticide` 一致的立场，双重关闭而非依赖红线事后拦截。
            return
        if result.items or result.unmatched_reason != _UNMATCHED:
            # 有证据就无需补充；带故障原因的未命中（"服务不可用"）也不兜底——那说明检索
            # 链路本身有问题，此时让模型补内容会掩盖故障。
            return
        try:
            basis = await asyncio.wait_for(
                self._call_fallback(state, ctx),
                timeout=get_settings().MODEL_FALLBACK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.warning("model fallback skipped", process_id=ctx.process_config.process_id.value, reason=str(exc))
            return
        if basis is None or not self._has_content(basis):
            return
        cast(dict[str, Any], state)["model_basis"] = basis.model_dump(mode="json")
        metrics.incr(_FALLBACK_METRIC, tags={"process_id": ctx.process_config.process_id.value})
        log.info("model fallback used", process_id=ctx.process_config.process_id.value)

    @staticmethod
    def _fallback_enabled() -> bool:
        """读兜底开关。延迟到调用点读而不是在 `__init__` 缓存：开关是运维期环境变量，
        进程内测试会反复改它（`get_settings.cache_clear()` 后可立即生效）。"""
        return bool(get_settings().ALLOW_MODEL_FALLBACK)

    @staticmethod
    def _has_content(basis: ModelBasis) -> bool:
        """四条清单全空视为没有可用的兜底产出，不必写进 state。

        空产出写进去会让下游多一次"读到空对象"的分支判断，收益为零。
        """
        return bool(
            basis.suggested_pesticide_names
            or basis.basis_points
            or basis.observation_suggestions
            or basis.help_suggestions
        )

    async def _call_fallback(self, state: ConsultationState, ctx: AgentContext) -> ModelBasis | None:
        """一次兜底调用。响应带错误或解析为空时返回 `None`。

        复用检索档位（`TaskType.RETRIEVE`，中等预算）：兜底是"查依据"的备用手段，与检索
        同属该环节，不单独开档位也不用高价档位——它产出的只是文本提示，不承担事实准确性。
        """
        messages = build_model_fallback_prompt(ctx.process_config, state)
        response = await self._require_gateway().chat(cast(GatewayTaskType, self.task_type), messages, ModelBasis)
        if response.error or response.parsed is None:
            return None
        basis = ModelBasis.model_validate(response.parsed)
        # `process_id` 由代码强制对齐当前流程，不信模型给的值：兜底产出只在同一流程内可复用，
        # 而模型在枚举字段上常给出无关流程（mock 供应商恒定回第一个枚举值）。留一手核对是
        # 因为跨流程套用会重演"术语与判据不能跨流程混用"的问题。
        if basis.process_id is not ctx.process_config.process_id:
            basis = basis.model_copy(update={"process_id": ctx.process_config.process_id})
        return basis

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> EvidenceSet:
        """降级：返回空集并标注未命中。

        降级不代表流程中断：判定拿到空集后应输出 `confidence_level="uncertain"` 与观察建议，
        处方应给出 `requires_tech_station=True`。`region_level_actual="none"` 是空集的正确
        标注，界面据此说明"暂时查不到适合您当地的条目"。
        """
        return EvidenceSet(
            query=self.build_query(state, ctx),
            items=[],
            hit=False,
            region_level_actual="none",
            reranked=False,
            unmatched_reason=f"证据核查服务不可用（{reason}）",
            fallback_chain=[],
        )


__all__ = [
    "KnowledgeRetriever",
    "RegionMatcher",
    "RetrieverAgent",
    "build_rewrite_prompt",
]
