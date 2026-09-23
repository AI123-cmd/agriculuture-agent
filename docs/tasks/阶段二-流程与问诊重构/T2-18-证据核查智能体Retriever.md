# T2-18 证据核查智能体 Retriever

## 这一项要做什么

在 `app/agent/agents/retriever.py` 中实现证据核查智能体，把已确认槽位与流程、地区信息改写为检索请求，调用知识库混合检索取得证据集，输出 `EvidenceSet`。它是"查依据"这一步的实现者，也是"模板出事实、模型只改写"原则的上游：处方里的药剂与剂量事实都来自这里检索到的已核准条目。 本智能体的失败行为是返回空集并标注未命中，而不是编造证据。空集是合法输出，下游判定的正确反应是降低确定性表述并给出观察建议。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`AgentContext`；依赖 T2-50：必须已提供地区回退匹配与来源标注
- 依赖 T2-10：必须已提供 `EvidenceSet`、`EvidenceQuery`、`EvidenceItem`；依赖 T2-49：必须已提供 `KnowledgeRepository` 混合检索接口（本项通过协议注入，不直接导入实现）
- 依赖 T2-02：必须已提供流程配置的 `terminology`（检索用词要过 `internal_terms`）；外部环境：pgvector 扩展与知识条目数据由 T2-47 至 T2-52 提供

## 参考章节

- 技术方案 第5章 5.1 智能体总表（④证据核查，自治程度中，失败返回空集并标注未命中）；技术方案 第5章 5.3.3 追问候选池的构造（检索改写属模型能力范围）
- 技术方案 第9章 9.2 知识库分层（四层知识与各自关键字段）；技术方案 第9章 9.3 检索侧的地区回退（县级到市级到省级到全国，保留实际命中级）
- 技术方案 第9章 9.3 `process_id` 是硬过滤而非软排序（不能跨流程混用术语与判据）；技术方案 第5章 5.3.5 处方契约（`pesticide_items` 只能由代码从知识库检索结果填充）
- 技术方案 第4章 4.4 模型分层策略（证据核查用中等模型加重排序模型）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/retriever.py` | `RetrieverAgent` 与检索改写 |
| 新增 | `app/agent/agents/prompts/retriever.py` | 查询改写提示词 |
| 新增 | `tests/agent/test_retriever_agent.py` | 硬过滤、地区回退、空集标注的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `RetrieverAgent` |

## 实现要点

### 类骨架与依赖注入

```python
class RetrieverAgent(BaseAgent[EvidenceSet]):
    name = "retriever"
    output_schema = EvidenceSet
    task_type = TaskType.RETRIEVE
    failure_mode = FailureMode.DEGRADE          # 返回空集并标注未命中
    max_attempts = 1                            # 检索不需要重试语义
    def __init__(
        self,
        gateway: ModelGateway,
        knowledge: KnowledgeRetriever,
        region_matcher: RegionMatcher,
    ) -> None:
        super().__init__(gateway)
        self._knowledge = knowledge
        self._region = region_matcher
```

`KnowledgeRetriever` 与 `RegionMatcher` 是 `typing.Protocol` 定义的协议，不是具体类。原因是 L2 规则要求 `app/agent/` 不得直接导入 ORM，且智能体要能被喂假数据单测。协议的声明如下：

```python
class KnowledgeRetriever(Protocol):
    async def search(
        self,
        query: EvidenceQuery,
        region_chain: list[str],
    ) -> list[EvidenceItem]: ...

class RegionMatcher(Protocol):
    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        """返回 (行政级别, 编码) 的有序回退链，从区县到全国。"""
```

`max_attempts = 1` 是刻意设置为不重试。检索失败的正确处置是返回空集并让判定降级，而不是重试三次再失败——重试三次会把问诊延迟拉长十几秒，而检索失败通常是知识库不可用这类不会立刻恢复的原因。

### 检索请求的构造

```python
    def build_query(self, state: ConsultationState, ctx: AgentContext) -> EvidenceQuery:
        """把槽位与流程信息压成检索请求。process_id 为硬过滤。"""
        config = ctx.process_config
        keywords = self._extract_keywords(state, config)
        return EvidenceQuery(
            process_id=config.process_id,
            keywords=keywords,
            region_code=ctx.region_code,
            slot_snapshot=slot_summary(state),
            kind_filter=[],
            top_k=self._top_k_for(config),
        )
```

`kind_filter` 默认空表示不限定条目类型。油菜流程是一个例外：技术方案 9.5 要求把 L3 处置知识的检索开关设为 `disabled`，通过红线 `RAPE-01` 拦截药剂推荐。因此油菜流程的 `kind_filter` 应排除 `pesticide`：

```python
    def _top_k_for(self, config: ProcessConfig) -> int:
        return 10 if config.panel_enabled else 8
    def kind_filter_for(self, config: ProcessConfig) -> list[str]:
        if config.process_id is ProcessId.RAPESEED:
            return ["pest", "disease", "cultivation", "threshold"]   # 排除 pesticide
        return []
```

油菜排除 `pesticide` 是"检索开关设为 disabled"的具体落实。这一层过滤是在候选集层面，比红线拦截更早生效；红线作为第二道防线保留。两道防线并存是为了防止知识库中误入油菜药剂条目。 `top_k` 对启用并行会诊的流程取 10，因为三条视角各自需要足够的证据空间；其余取 8。

### 关键词抽取

```python
    def _extract_keywords(self, state: ConsultationState, config: ProcessConfig) -> list[str]:
        """关键词来源：已填槽位值、流程内部术语、易混淆项候选。"""
        words: list[str] = []
        for slot in (state.get("slots") or {}).values():
            if slot.status == "filled" and slot.value:
                words.append(slot.value)
        for term in config.terminology.display_metrics:
            words.append(term)
        for pair in config.confusable_items:
            words.extend(pair.candidates)
        observations = state.get("observations") or {}
        for value in (observations.get("observed_facts") or {}).values():
            if value:
                words.append(value)
        return _dedupe(words)
```

关键词里必须包含流程的 `display_metrics` 与易混淆项候选。前者保证"茶流程用叶发病率检索而不是株发病率"，后者保证涉及鉴别的查询能召回判据类条目。这两项都来自流程配置，不硬编码。 `_dedupe` 保序去重，不排序。保序的意义是让高优先来源（用户回答）排在前面，检索侧若做关键词权重衰减时能利用这个顺序。

### 查询改写的模型使用

```python
    async def rewrite_keywords(self, keywords: list[str], config: ProcessConfig) -> list[str]:
        """用模型把口语化表述改写成检索用词。失败则原样返回。"""
        if not keywords:
            return []
        try:
            rewritten = await self._rewrite_once(keywords, config)
        except Exception as exc:                       # noqa: BLE001
            log.warning("keyword rewrite failed, use raw", extra={"reason": str(exc)})
            return keywords
        return _dedupe([*keywords, *rewritten])
```

改写是**增量**的：模型产出的词追加在原词之后，而不是替换。这样即使模型改写偏离，原始表述仍在请求里，检索不会因为一次失败的改写而完全走偏。改写失败时静默返回原词，不触发降级，因为这是一个可选的增强步骤。 改写提示词要求模型按流程术语规范输出，例如把农户说的"叶子起油"改写为"茶饼病"，把"蔫了"改写为"萎蔫"：

```python
def build_rewrite_prompt(keywords: list[str], config: ProcessConfig) -> list[dict]:
    system = [
        "你是农业检索词改写器。把农户口语改写成植保检索用词。",
        f"本流程的标准术语：{json.dumps(config.terminology.internal_terms, ensure_ascii=False)}",
        "只输出改写后的词，用顿号分隔。不要解释。",
    ]
    return [{"role": "system", "content": "\n".join(system)},
            {"role": "user", "content": "、".join(keywords)}]
```

改写任务用小模型即可（不改写事实，只做词汇映射），通过 `TaskType.RETRIEVE` 的路由配置指定中等档位。

### 检索与地区回退

```python
    async def run_search(self, query: EvidenceQuery, ctx: AgentContext) -> tuple[list[EvidenceItem], str, list[str]]:
        """按地区回退链逐级检索，命中即止。返回证据、实际命中级别与尝试链。"""
        chain = self._region.build_chain(query.region_code)
        attempted: list[str] = []
        for level, code in chain:
            attempted.append(f"{level}:{code}")
            scoped = query.model_copy(update={"region_code": code})
            items = await self._knowledge.search(scoped, region_chain=[c for _, c in chain])
            if items:
                return items, level, attempted
        return [], "none", attempted
```

回退链由 `RegionMatcher` 构造，顺序为县级、市级、省级、全国。检索实现（T2-49）内部接收整条链用于相关性加权，而回退的判定循环在这里：某一级召回非空即停止。 技术方案 9.3 要求"每次回退都在 `EvidenceItem.region_scope` 中保留实际命中的级别"。因此返回的证据其 `region_scope` 由检索实现填写为实际命中的编码，这里的 `level` 是本次回退链的终点级别，写入 `EvidenceSet.region_level_actual`。

### 硬过滤的自检

```python
    def enforce_process_scope(self, items: list[EvidenceItem], process_id: ProcessId) -> list[EvidenceItem]:
        """二次硬过滤：丢弃跨流程条目。检索侧过滤之外的自检。"""
        kept: list[EvidenceItem] = []
        for item in items:
            if item.process_id == process_id.value or item.process_id == ProcessId.GENERAL.value:
                kept.append(item)
            else:
                log.warning("cross-process evidence dropped",
                            extra={"knowledge_id": item.knowledge_id, "process_id": item.process_id})
        return kept
```

通用流程的条目允许混入任意流程，因为它本身就是兜底知识。其他流程的条目严格限定在自身。这层过滤与 T2-10 契约里的 `_all_items_same_process` 校验器形成双保险：契约层拦的是结构性错误，这里拦的是检索侧的疏漏。

### 组装 EvidenceSet

```python
    async def post_validate(self, result: EvidenceSet, state: ConsultationState, ctx: AgentContext) -> EvidenceSet:
        """补充未命中原因、去重、限制条数。"""
        unique = {item.knowledge_id: item for item in result.items}
        items = sorted(unique.values(), key=lambda i: i.score, reverse=True)[: self._top_k_for(ctx.process_config)]
        updates: dict[str, Any] = {"items": items, "hit": bool(items)}
        if not items and not result.unmatched_reason:
            updates["unmatched_reason"] = "知识库未命中该流程与地区的相关条目"
        return result.model_copy(update=updates)
```

去重按 `knowledge_id` 保留分值更高者，因为混合检索（BM25 加向量）的不同通路可能召回同一条目。排序后截断到 `top_k`，与检索请求的 `top_k` 呼应，防止检索实现返回超量结果。

### 降级实现

```python
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> EvidenceSet:
        """降级：返回空集并标注未命中。"""
        query = self.build_query(state, ctx)
        return EvidenceSet(
            query=query,
            items=[],
            hit=False,
            region_level_actual="none",
            reranked=False,
            unmatched_reason=f"证据核查服务不可用（{reason}）",
            fallback_chain=[],
        )
```

降级不代表流程中断：判定智能体拿到空集后应输出 `confidence_level="uncertain"` 与观察建议，处方智能体应给出 `requires_tech_station=True`。这两条逻辑分别在 T2-19 与 T2-21 实现。 `region_level_actual="none"` 是空集的正确标注，界面据此可以说明"暂时查不到适合您当地的条目"。

## 验收标准

- `pytest tests/agent/test_retriever_agent.py` 全部通过，含三类用例：混入其他流程的条目被丢弃、县级无结果时回退到市级并在 `fallback_chain` 中记录两级、检索失败时返回空集且 `unmatched_reason` 非空；断言 `EvidenceQuery.process_id` 恒等于 `ctx.process_config.process_id`
- 断言油菜流程的 `kind_filter_for` 不包含 `pesticide`；断言关键词改写失败时返回原始关键词且不抛异常（用 mock 网关抛错验证）
- 断言 `rewrite_keywords` 的输出包含全部原始关键词（增量而非替换）；断言同一份证据集在 `post_validate` 前后按 `knowledge_id` 去重，且条数不超过 `top_k`
- 断言降级返回的 `hit` 为 False 且 `region_level_actual` 为 `"none"`；断言 `USE_MOCK_LLM=true` 下测试不产生真实网络请求
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
