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

from sqlalchemy import ColumnElement, Integer, Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.status import SEARCHABLE_STATUSES, KnowledgeStatus
from app.models.knowledge import KnowledgeItem

# 关键词召回时，单个分词过短（如单字）区分度太低，容易把无关条目拉进来。过滤长度由业务层
# 决定，这里只做 SQL；常量放在此处表示"数据访问层也认可这个下限"。
_MIN_TOKEN_LEN = 2


def _escape_like(token: str) -> str:
    """转义 LIKE 通配符。农户口语与药名里出现的 `%`、`_` 不应被当成通配符。"""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_any(column, token: str) -> ColumnElement[bool]:
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


__all__ = ["KnowledgeRepository"]
