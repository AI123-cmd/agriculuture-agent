"""知识条目仓储：T2-47 提供基础读取（按 id 批量取、按状态取），T2-49 追加混合检索的两路召回。

本仓储与归属类仓储形态不同——知识条目是全局只读数据，没有 `user_id` 归属概念
（与 `ProcessConfigRepository` 同类），故构造时绑定会话、方法不接受 `user_id`。

这里只放"把一条 SQL 打出去、取回候选"的数据访问职责：过滤条件由 `app/knowledge/filters.py`
构造后传入，RRF 融合、重排序、证据组装都在 `app/knowledge/repository.py`（业务编排）。
把 SQL 与编排分层，是为了让"过滤条件"与"检索流程"能被分别单测。

按状态取**默认只放行可检索状态**（`SEARCHABLE_STATUSES`）：调用方要查 `draft` /
`suspended` 必须显式传入，避免"顺手查全部状态"把未核准条目带进检索结果。
"""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, Select, case, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.knowledge.status import SEARCHABLE_STATUSES, KnowledgeStatus
from app.models.knowledge import KnowledgeItem

# 关键词召回时，单个分词过短（如单字）区分度太低，容易把无关条目拉进来。过滤长度由业务层
# 决定，这里只做 SQL；常量放在此处表示"数据访问层也认可这个下限"。
_MIN_TOKEN_LEN = 2


def _escape_like(token: str) -> str:
    """转义 LIKE 通配符。农户口语与药名里出现的 `%`、`_` 不应被当成通配符。"""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_any(column: InstrumentedAttribute[str], token: str) -> ColumnElement[bool]:
    """单个分词的“包含”匹配，显式指定转义符与 SQLAlchemy 默认保持一致。"""
    return column.ilike(f"%{_escape_like(token)}%", escape="\\")


class KnowledgeRepository:
    """知识条目读取入口。构造时绑定会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_ids(self, knowledge_ids: Sequence[str]) -> list[KnowledgeItem]:
        """按 id 批量取回条目，保持乱序取回后由调用方按需重排。

        传入空列表直接返回空列表：避免生成 `WHERE knowledge_id IN ()` 这种非法 SQL。
        """
        if not knowledge_ids:
            return []
        stmt = select(KnowledgeItem).where(KnowledgeItem.knowledge_id.in_(list(knowledge_ids)))
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, knowledge_id: str) -> KnowledgeItem | None:
        """按主键取单条，不存在返回 None。"""
        stmt = select(KnowledgeItem).where(KnowledgeItem.knowledge_id == knowledge_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, knowledge_id: str) -> KnowledgeItem | None:
        """`get` 的别名，供增量构建等读起来更明确的调用点使用。"""
        return await self.get(knowledge_id)

    async def list_by_status(
        self,
        status: KnowledgeStatus | str = KnowledgeStatus.APPROVED,
        *,
        process_id: str | None = None,
        limit: int | None = None,
    ) -> list[KnowledgeItem]:
        """按状态取条目，可选按流程过滤。

        默认状态是唯一可检索的 `approved`；要查未核准状态须显式传入。`status` 接受
        枚举或字符串，落库比较统一取 `.value`，避免枚举与字符串比较出现漏配。
        """
        raw = status.value if isinstance(status, KnowledgeStatus) else status
        stmt = select(KnowledgeItem).where(KnowledgeItem.status == raw)
        if process_id is not None:
            stmt = stmt.where(KnowledgeItem.process_id == process_id)
        stmt = stmt.order_by(KnowledgeItem.knowledge_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_searchable(self, *, process_id: str | None = None) -> list[KnowledgeItem]:
        """取全部可检索条目。可检索集合由 `SEARCHABLE_STATUSES` 单一常量决定。"""
        stmt = select(KnowledgeItem).where(KnowledgeItem.status.in_([s.value for s in SEARCHABLE_STATUSES]))
        if process_id is not None:
            stmt = stmt.where(KnowledgeItem.process_id == process_id)
        stmt = stmt.order_by(KnowledgeItem.knowledge_id)
        return list((await self._session.execute(stmt)).scalars().all())

    # ---- 混合检索的两路召回（T2-49）----
    #
    # 这里只负责把 SQL 打出去：过滤条件（`where` 子句）由 `app/knowledge/filters.py` 构造后
    # 传入，融合与组装在 `app/knowledge/repository.py`。两路均返回 `(条目, 原始分)` 列表，
    # 分值尺度不同，故只在各自通路内有意义，跨路比较由 RRF 按名次完成。

    def vector_recall_stmt(self, vector: list[float], clauses: Sequence[ColumnElement[bool]], top_k: int) -> Select:
        """向量召回语句：pgvector 余弦距离，元数据过滤在同一条 SQL 内（技术方案 4.3）。

        `embedding IS NOT NULL` 是防御性的：索引构建器已保证写入的条目都有向量，但历史
        数据或人工插入可能留下空向量，空向量参与距离计算会得到无意义结果。余弦距离越小越
        相似，故按 `distance` 升序；`knowledge_id` 作次序键，保证同距离时结果确定。
        """
        distance = KnowledgeItem.embedding.cosine_distance(vector).label("distance")
        return (
            select(KnowledgeItem, distance)
            .where(*clauses)
            .where(KnowledgeItem.embedding.is_not(None))
            .order_by(distance, KnowledgeItem.knowledge_id)
            .limit(top_k)
        )

    async def vector_recall(
        self, vector: list[float], clauses: Sequence[ColumnElement[bool]], top_k: int
    ) -> list[tuple[KnowledgeItem, float]]:
        """执行向量召回。返回 `(条目, 相似度)`，相似度 = 1 - 余弦距离，落在 0~1。"""
        rows = (await self._session.execute(self.vector_recall_stmt(vector, clauses, top_k))).all()
        return [(item, 1.0 - float(distance)) for item, distance in rows]

    def keyword_recall_stmt(self, tokens: Sequence[str], clauses: Sequence[ColumnElement[bool]], top_k: int) -> Select:
        """关键词召回语句：按分词命中数与命中字段加权打分。

        环境未装 `zhparser`，`tsvector('simple', ...)` 会把整段中文当成一个词条，`@@` 对中文
        查询恒不命中；`pg_trgm` / `pg_bigm` 也未安装。因此关键词通路改为**分词后逐个包含匹配**：
        用项目已有的 `jieba` 切词，SQL 侧统计命中词数与命中字段。命中标题权重大于正文（标题更
        能代表条目主题），分值标签沿用 `rank`，使两路语句形态与合同一致、便于断言。

        `token` 均小于下限时没有任何过滤条件会命中，语句退化为空结果——这由调用方在分词阶段
        处理（无有效分词则不发起关键词召回），本语句不做隐式兜底。
        """
        score = sum(
            (
                case((_like_any(KnowledgeItem.title, token), 2), else_=0)
                + case((_like_any(KnowledgeItem.content, token), 1), else_=0)
                for token in tokens
            ),
            start=literal(0),
        ).label("rank")
        matched = or_(
            *[_like_any(KnowledgeItem.title, t) for t in tokens], *[_like_any(KnowledgeItem.content, t) for t in tokens]
        )
        return (
            select(KnowledgeItem, score)
            .where(*clauses)
            .where(matched)
            .order_by(score.desc(), KnowledgeItem.knowledge_id)
            .limit(top_k)
        )

    async def keyword_recall(
        self, tokens: Sequence[str], clauses: Sequence[ColumnElement[bool]], top_k: int
    ) -> list[tuple[KnowledgeItem, float]]:
        """执行关键词召回。返回 `(条目, 命中得分)`；无有效分词时不发查询。"""
        valid = [t for t in tokens if len(t) >= _MIN_TOKEN_LEN]
        if not valid:
            return []
        rows = (await self._session.execute(self.keyword_recall_stmt(valid, clauses, top_k))).all()
        return [(item, float(rank or 0.0)) for item, rank in rows]


__all__ = ["KnowledgeRepository"]
