# T2-49 混合检索 KnowledgeRepository

## 这一项要做什么

在 `app/knowledge/repository.py` 中实现 `KnowledgeRepository.search`：把技术方案 9.3 给出的五步检索骨架落到可运行代码上，即元数据强过滤、并行两路召回、RRF 融合、重排序、组装带溯源的证据。它是证据核查智能体与处方智能体的取数底层，也是"药剂白名单"等一系列闸门规则在检索侧的落实点。

检索的第一原则是 `process_id` 硬过滤：茶流程的查询绝不召回辣椒的药剂条目。这一条要在候选集层面消除，而不是靠后续重排序纠正。

## 前置依赖

- 依赖 T2-47：必须已提供 `knowledge_item`、`knowledge_region` 表与 `KnowledgeStatus`
- 依赖 T2-52：必须已提供一期五条流程的知识条目数据，否则检索无内容可召回
- 依赖 T2-48：必须已提供索引构建器与 `Embedder`，`embedding` 列已填充
- 依赖 T2-01：必须已提供 `ProcessId`、`EvidenceKind`；依赖 T2-10：必须已提供 `EvidenceQuery`
- 外部环境：PostgreSQL 全文检索（`tsvector` 配 `zhparser`，或以 `pg_bigm`/`pg_trgm` 兜底）；pgvector HNSW 索引
- 与 T2-50 的关系：地区回退的逐级匹配由 T2-50 调用本项实现；与 T2-51 的关系：重排序默认走轻量实现，cross-encoder 接入在 T2-51

## 参考章节

- 技术方案 第9章 9.3 混合检索实现（五步骨架原样保留）
- 技术方案 第9章 9.3 为什么 process_id 是硬过滤而不是软排序
- 技术方案 第9章 9.3 `_region_match` 的回退规则（县级到市级到省级到全国）
- 技术方案 第9章 9.2 知识库分层（L3 处置知识是红线的基础）
- 技术方案 第4章 4.3 为什么是 PostgreSQL 统一栈（一条 SQL 内完成向量排序与元数据过滤）
- 技术方案 第5章 5.3.1 共享状态中的 EvidenceItem（证据条目字段）
- 技术方案 第8章 8.2 规则清单（药剂白名单以 approved 为准）
- 附录 C.2 技术术语（BM25、RRF、cross-encoder 的约定含义）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/knowledge/repository.py` | `KnowledgeRepository`：五步检索 |
| 新增 | `app/knowledge/fusion.py` | `reciprocal_rank_fusion`、`dedupe_by_id` 纯函数 |
| 新增 | `app/knowledge/filters.py` | `build_filters`：元数据过滤条件构造 |
| 新增 | `app/knowledge/errors.py` | `KnowledgeUnavailableError` |
| 新增 | `tests/knowledge/test_repository_search.py` | 硬过滤、并行召回、融合、组装的用例 |
| 修改 | `app/repositories/knowledge.py` | 新增检索所需的向量与关键词查询方法 |

## 实现要点

### search 的五步骨架

严格按技术方案 9.3 保留步骤划分与顺序，实现时只把占位方法补全：

```python
class KnowledgeRepository:
    """知识检索：BM25 + 向量混合，元数据强过滤，重排序。"""

    async def search(self, query: KnowledgeQuery) -> list[EvidenceItem]:
        # 1. 元数据过滤（SQL 层，先缩小候选集）
        filters = self._build_filters(query)

        # 2. 并行两路召回
        vector_task = self._vector_recall(query.text, filters, top_k=20)
        keyword_task = self._keyword_recall(query.text, filters, top_k=20)
        vector_hits, keyword_hits = await asyncio.gather(vector_task, keyword_task)

        # 3. RRF 融合
        fused = self._reciprocal_rank_fusion(vector_hits, keyword_hits)

        # 4. 重排序（cross-encoder 或轻量模型）
        reranked = await self._rerank(query.text, fused[:30], top_k=query.top_k)

        # 5. 组装证据（带 provenance）
        return [self._to_evidence(item, query) for item in reranked]

    def _build_filters(self, query: KnowledgeQuery) -> dict:
        """强过滤条件：流程、状态、地区、生效期、目标。"""
        return {
            "process_id": query.process_id,          # 硬过滤，绝不跨流程
            "status": "approved",                    # 仅已核准
            "region_scope": self._region_match(query.region_code),
            "effective_now": True,
            "kind": query.kinds,
        }
```

五步的顺序不可调整。元数据过滤必须在召回之前，因为向量召回的 `top_k=20` 是有限预算，若先召回再过滤，跨流程与未核准条目会占满候选空间，真正可用的条目被挤出。并行两路召回用 `asyncio.gather` 而不是顺序执行，因为两路互不依赖；串行会把延迟从"两者较大者"变成"两者之和"。RRF 融合后再重排序而不是先重排再融合，因为 RRF 是无参的排序合并，先融合能把两路各自的前排候选汇成一份更全的候选，交给计算更贵的重排处理。

`_build_filters` 返回的是过滤描述而不是直接拼 SQL，让 T2-51 与 T2-50 能复用同一份条件集，也让条件集可被单测断言（断言字典内容比断言 SQL 字符串稳定）。

### process_id 硬过滤而非软排序的理由

产品说明书第五章反复强调术语与判据不能跨流程混用。典型的失败模式是"在茶农面前说病株率"——茶流程用叶发病率，若召回了水稻或辣椒的条目，输出的指标口径就错了。

只做软排序时，茶流程的查询表达式里含"病""叶"这类通用词，向量与关键词两路都会给辣椒条目打分，且分数不一定低于茶条目（辣椒条目的正文可能更贴近查询措辞）。软排序把纠正的责任交给重排序模型，而重排序是在同一个候选池内比较，若候选池里根本没有正确的茶条目，重排也无力回天。

硬过滤从候选集层面消除这个风险：过滤条件在 SQL 的 `WHERE` 中执行，跨流程条目在进入 `top_k` 之前就被排除。这条与 T2-18 的证据核查契约里的 `_all_items_same_process` 校验器形成双保险，前者是检索实现的强制，后者是契约层的自检。

通用流程 `general` 的条目允许混入任意流程，因为它是兜底知识（无专属阈值、无药剂条目）。实现方式是在过滤条件里把 `process_id` 展开为 `[query.process_id, ProcessId.GENERAL]` 两者，而不是放宽为不过滤。

### 两路召回

```python
    async def _vector_recall(self, text: str, filters: dict, top_k: int) -> list[KnowledgeHit]:
        """向量召回：pgvector 余弦距离。与元数据过滤在同一条 SQL 内。"""
        vector = (await self._embedder.embed([text]))[0]
        stmt = (select(KnowledgeItem, KnowledgeItem.embedding.cosine_distance(vector).label("distance"))
            .where(*self._apply(filters))
            .where(KnowledgeItem.embedding.is_not(None))
            .order_by("distance")
            .limit(top_k))
        rows = (await self._session.execute(stmt)).all()
        return [KnowledgeHit(row=item, score=1.0 - float(distance), channel="vector") for item, distance in rows]

    async def _keyword_recall(self, text: str, filters: dict, top_k: int) -> list[KnowledgeHit]:
        """关键词召回：PG 全文检索，按 ts_rank 排序。"""
        stmt = (select(KnowledgeItem, func.ts_rank(KnowledgeItem.search_vector, func.plainto_tsquery("simple", text)).label("rank"))
            .where(*self._apply(filters))
            .where(KnowledgeItem.search_vector.op("@@")(func.plainto_tsquery("simple", text)))
            .order_by(text("rank DESC"))
            .limit(top_k))
        rows = (await self._session.execute(stmt)).all()
        return [KnowledgeHit(row=item, score=float(rank or 0.0), channel="keyword") for item, rank in rows]
```

中文全文检索需要 `zhparser` 分词扩展；若环境未装，退化为 `pg_bigm` 或 `pg_trgm` 的相似度匹配。`search_vector` 是 `knowledge_item` 上的一个生成列（`title` 与 `content` 的加权 `tsvector`），由迁移建立并在写入时自动更新，不由应用层维护。两路各自独立取 `top_k=20`，互不影响；任一路为空不视为检索失败，只要另一路有结果就可继续。

向量召回里 `embedding.is_not(None)` 的条件是防御性的：索引构建器已保证写入的条目都有向量，但历史数据或人工插入可能留下空向量，空向量参与距离计算会得到无意义结果。

### RRF 融合

```python
def reciprocal_rank_fusion(a: list[KnowledgeHit], b: list[KnowledgeHit], k: int = 60) -> list[KnowledgeHit]:
    """倒数排名融合：按排名而非分值合并两路结果。"""
    scores: dict[str, float] = {}
    kept: dict[str, KnowledgeHit] = {}
    for hits in (a, b):
        for rank, hit in enumerate(hits, start=1):
            kid = hit.row.knowledge_id
            scores[kid] = scores.get(kid, 0.0) + 1.0 / (k + rank)
            kept.setdefault(kid, hit)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [kept[kid].with_score(score) for kid, score in ordered]
```

RRF 用排名而不是原始分值，因为两路的分数尺度不同：向量余弦相似度在 0 到 1 之间，`ts_rank` 的取值范围不固定且依赖查询词频。直接加权求和需要为两路各调一个权重，而权重随查询变化需要反复调参。RRF 只依赖名次，无需归一化也无需权重，代价是丢失了分差信息，因此融合后的分数只用于排序，不对外暴露为"相关度"。

同一条目被两路同时召回时，RRF 自然给它更前的位置（两个排名贡献叠加），这正是混合检索想要的效果：语义与关键词同时命中通常意味着该条目确实相关。

### 组装证据

```python
    def _to_evidence(self, hit: KnowledgeHit, query: KnowledgeQuery) -> EvidenceItem:
        """组装证据条目。字段逐项照抄，score 用融合后的分值。"""
        row = hit.row
        return EvidenceItem(knowledge_id=row.knowledge_id, title=row.title, content=row.content,
            process_id=row.process_id, region_scope=row.region_scope or "national",
            effective_date=row.effective_date, kind=row.kind, score=hit.score,
            pesticide_name=row.pesticide_name, registered_crops=list(row.registered_crops or []),
            dosage_min=row.dosage_min, dosage_max=row.dosage_max, dosage_unit=row.dosage_unit,
            safety_interval_days=row.safety_interval_days,
            max_uses_per_season=row.max_uses_per_season,
            forbidden_combinations=list(row.forbidden_combinations or []),
            source_ref=row.source_ref, )
```

`_to_evidence` 逐字段照抄，不做任何换算或推断。下游的处方智能体按 `_to_pesticide_item` 再照抄一次，两次照抄共同保证"模板出事实"。`region_scope` 为空时填 `"national"` 而不是 `None`，与 T2-10 契约里 `region_level_actual` 的 `national` 取值对齐。

### 降级与错误处理

检索真不可用（数据库连接失败、向量化服务超时）时抛 `KnowledgeUnavailableError`，由调用方 T2-18 捕获并返回空集。检索本身不做重试：向量化服务若超时，立即重试大概率仍超时，反而拉长问诊延迟。需要重试的语义在上层（证据核查智能体）统一处理。

## 验收标准

- `pytest tests/knowledge/test_repository_search.py` 全部通过，至少含七条：`process_id=tea` 的查询结果中不含任何 `process_id=chili` 或 `rice` 的条目、`status='suspended'` 的条目不出现在结果中、`status='draft'` 的条目不出现在结果中、两路同时命中的条目排名高于仅单路命中的条目、`kind` 过滤生效、`general` 条目可混入其他流程结果、有效期外的条目被排除
- `pytest tests/knowledge/test_fusion.py` 全部通过，含：RRF 对两路完全相同的列表保持原顺序、对仅在单路出现的条目给出非零分、`k=60` 时首名分值为 `1/61` 加另一路贡献
- 断言 `_build_filters` 对 `process_id=tea` 返回的字典中 `process_id` 为 `[ProcessId.TEA, ProcessId.GENERAL]` 且 `status` 为 `"approved"`
- 断言向量召回 SQL 与关键词召回 SQL 均带 `WHERE status='approved'` 与 `process_id IN (...)`（用 `str(stmt)` 断言）
- 断言 `_vector_recall` 的 `top_k` 与 `_keyword_recall` 的 `top_k` 均为 20，且重排序候选上限为 30
- 断言两路召回以 `asyncio.gather` 并发执行（用两个各阻塞 0.1 秒的假实现，总耗时低于 0.15 秒）
- 断言 `_vector_recall` 排除 `embedding IS NULL` 的条目
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
