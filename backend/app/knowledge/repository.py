"""知识检索：混合召回、融合、重排序、组装证据（T2-49，技术方案 9.3）。

把 9.3 的五步骨架落到可运行代码：

1. **元数据强过滤**（SQL 层，先缩小候选集，见 `app/knowledge/filters.py`）
2. **并行两路召回**（向量 + 关键词，各取 `RECALL_TOP_K`）
3. **RRF 融合**（按名次合并两路，见 `app/knowledge/fusion.py`）
4. **重排序**（默认轻量实现，cross-encoder 在 T2-51 接入）
5. **组装证据**（逐字段照抄，带溯源）

五步顺序不可调整。过滤必须在召回前：两路各自只取 20 个候选，是有限预算，先召回再过滤会让
跨流程与未核准条目占满候选空间。两路召回用 `asyncio.gather` 并发而非串行：两者互不依赖，
串行会把延迟从"两者较大者"变成"两者之和"。RRF 在重排序之前：RRF 是无参排序合并，先把两路
前列候选汇成更全的候选，再交给更贵的重排处理。

**本类持会话工厂而非单个会话**：`AsyncSession` 不是并发安全的，同一个会话上并发执行两条
语句会抛 `IllegalStateChangeError`。两路召回要真正并发（这是 `asyncio.gather` 的意义），
就必须各用一条独立会话——故构造时传入 `async_sessionmaker`，每次召回各开一条会话。

本模块是**业务编排**，SQL 在 `app/repositories/knowledge.py`（数据访问层）。两者分层是为了
让"过滤条件""融合逻辑""检索流程"能被分别单测。
"""

import asyncio
import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast

import jieba
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contracts import EvidenceItem, EvidenceQuery
from app.agent.contracts.evidence import EvidenceKindValue
from app.knowledge.embedding import Embedder
from app.knowledge.errors import KnowledgeUnavailableError
from app.knowledge.filters import build_filters, where_clauses
from app.knowledge.fusion import CHANNEL_KEYWORD, CHANNEL_VECTOR, KnowledgeHit, dedupe_by_id, reciprocal_rank_fusion
from app.knowledge.rerank import RERANK_CANDIDATE_LIMIT, Reranker, build_reranker
from app.knowledge.schema import NATIONAL_LEVEL
from app.repositories.knowledge import KnowledgeRepository as _QueryRepository

logger = logging.getLogger(__name__)

# 两路各自召回的候选上限（技术方案 9.3：top_k=20）。
RECALL_TOP_K = 20
# 传给重排序的候选上限。来自 T2-51 的 `rerank.RERANK_CANDIDATE_LIMIT`，此处重新导出便于
# 检索侧单测直接断言"交给重排的候选数不超过 30"。
RERANK_LIMIT = RERANK_CANDIDATE_LIMIT


def _to_float(value: Decimal | None) -> float | None:
    """`Numeric` 列取回是 `Decimal`，证据契约用 `float`；None 原样透传。"""
    return None if value is None else float(value)


class KnowledgeRepository:
    """知识检索入口。构造时绑定会话工厂与向量化实现；重排序器可选注入（T2-51）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        reranker: Reranker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._rerank_impl: Reranker = reranker or build_reranker()

    async def search(self, query: EvidenceQuery) -> list[EvidenceItem]:
        """执行一次单级地区的混合检索，返回按相关度排序的证据。

        单级语义（"一个查询一个地区"）是刻意的：逐级回退是"哪一级够用"的决策，属 T2-50 的
        地区匹配职责。保持单级让本方法可被直接单测，也让回退链的构造与检索实现解耦。
        """
        try:
            return await self._search(query)
        except KnowledgeUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001  数据库/向量服务故障统一转成检索不可用
            raise KnowledgeUnavailableError(str(exc)) from exc

    async def _search(self, query: EvidenceQuery) -> list[EvidenceItem]:
        filters = self._build_filters(query)
        clauses = where_clauses(filters)
        text = self._query_text(query)

        vector_hits, keyword_hits = await asyncio.gather(
            self._vector_recall(text, clauses, RECALL_TOP_K),
            self._keyword_recall(text, clauses, RECALL_TOP_K),
        )

        fused = reciprocal_rank_fusion(dedupe_by_id(vector_hits), dedupe_by_id(keyword_hits))
        reranked = await self._rerank_impl.rerank(text, fused[:RERANK_LIMIT], query.top_k)
        return [self._to_evidence(hit, query) for hit in reranked]

    # ---- 第 1 步：元数据过滤 ----

    def _build_filters(self, query: EvidenceQuery) -> dict:
        """强过滤条件描述。返回字典而非 SQL，便于复用与断言（见 `filters.build_filters`）。"""
        return build_filters(query)

    @staticmethod
    def _query_text(query: EvidenceQuery) -> str:
        """把检索用词拼成一路文本。两路召回共用同一份文本，语义前提一致。"""
        return " ".join(word for word in query.keywords if word).strip()

    # ---- 第 2 步：并行两路召回 ----
    #
    # 两路各自开一条会话：同一个 `AsyncSession` 上并发执行两条语句会抛
    # `IllegalStateChangeError`，故必须各用独立会话才能真正并发。

    async def _vector_recall(self, text: str, clauses: Sequence[Any], top_k: int) -> list[KnowledgeHit]:
        """向量召回：先向量化，再在独立会话里按余弦距离取候选。

        文本为空时跳过（空串向量化没有语义）。向量化是网络调用，与关键词路的 SQL 并发执行，
        这正是 `asyncio.gather` 要重叠的部分。
        """
        if not text:
            return []
        vector = (await self._embedder.embed([text]))[0]
        async with self._session_factory() as db:
            rows = await _QueryRepository(db).vector_recall(vector, clauses, top_k)
        return [KnowledgeHit(row=row, score=score, channel=CHANNEL_VECTOR) for row, score in rows]

    async def _keyword_recall(self, text: str, clauses: Sequence[Any], top_k: int) -> list[KnowledgeHit]:
        """关键词召回：jieba 分词后逐个包含匹配（环境无 zhparser，见数据访问层注释）。"""
        tokens = self._tokenize(text)
        async with self._session_factory() as db:
            rows = await _QueryRepository(db).keyword_recall(tokens, clauses, top_k)
        return [KnowledgeHit(row=row, score=score, channel=CHANNEL_KEYWORD) for row, score in rows]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """jieba 分词并保序去重。分词在进程内确定，满足检索可复现。"""
        seen: set[str] = set()
        out: list[str] = []
        for token in jieba.lcut(text):
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                out.append(token)
        return out

    # ---- 第 5 步：组装证据 ----

    def _to_evidence(self, hit: KnowledgeHit, query: EvidenceQuery) -> EvidenceItem:
        """逐字段照抄成证据条目，不做任何换算或推断。

        "模板出事实、模型只改写"依赖两次照抄：这里照抄一次（知识库 → 证据），处方智能体
        （T2-21 `_to_pesticide_item`）再照抄一次（证据 → 处方）。任一环节做"推断"，事实就
        有被模型生成的风险。

        `region_scope` 为空（该条目为全国通用）时填 `"national"`，与 T2-10 契约里
        `region_level_actual` 的取值对齐。带级别的精确标注（`"city:330100"`）由 T2-50 回退
        匹配写回。
        """
        row = hit.row
        return EvidenceItem(
            knowledge_id=row.knowledge_id,
            title=row.title,
            content=row.content,
            process_id=row.process_id,
            region_scope=row.region_scope or NATIONAL_LEVEL,
            effective_date=row.effective_date,
            kind=cast(EvidenceKindValue, row.kind),
            score=hit.score,
            dosage_min=_to_float(row.dosage_min),
            dosage_max=_to_float(row.dosage_max),
            dosage_unit=row.dosage_unit,
            safety_interval_days=row.safety_interval_days,
            max_uses_per_season=row.max_uses_per_season,
            registered_crops=list(row.registered_crops or []),
            forbidden_combinations=list(row.forbidden_combinations or []),
        )


__all__ = ["RECALL_TOP_K", "RERANK_LIMIT", "KnowledgeRepository", "Reranker"]
