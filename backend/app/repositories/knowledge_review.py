"""知识条目状态变更记录仓储（T2-54）。

本仓储**只追加、只查询**：不提供任何更新或删除方法。不可变性由"方法集"表达——T2-54 的
验收用反射断言本类的方法名里不出现 update/delete/modify/remove，任何人日后加了这样的方法
都会被测试拦下。若某次操作填错，正确做法是追加一条纠正记录（把状态改回），而不是改历史行。

与 `KnowledgeRepository` 同类，构造时绑定会话（知识域是全局数据，无 `user_id` 归属概念），
方法不接受 `user_id`。写入行由 `KnowledgeReviewService` 构造后传入，本类不自行拼装业务字段。
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_review import KnowledgeReviewLog


class KnowledgeReviewLogRepository:
    """状态变更记录读写。只有追加与查询两类方法。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, log_row: KnowledgeReviewLog) -> KnowledgeReviewLog:
        """追加一条变更记录。

        只 `add` 不 `commit`：事务边界由调用方（`KnowledgeReviewService` 的
        `unit_of_work`）统一决定，"写日志 + 改主表"要么同成功、要么同回滚——这正是
        "每次成功迁移必有一条日志、且不会出现有日志无变更"的前提。
        """
        self._session.add(log_row)
        return log_row

    async def list_for_knowledge(self, knowledge_id: str) -> list[KnowledgeReviewLog]:
        """某条目的完整变更时间线，按时间升序（追溯历史用）。"""
        stmt = (
            select(KnowledgeReviewLog)
            .where(KnowledgeReviewLog.knowledge_id == knowledge_id)
            .order_by(KnowledgeReviewLog.created_at, KnowledgeReviewLog.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_by_operator(self, operator_id: str, *, limit: int | None = None) -> list[KnowledgeReviewLog]:
        """某后台账号的变更记录，按时间倒序（复盘"谁改了哪些"用）。"""
        stmt = (
            select(KnowledgeReviewLog)
            .where(KnowledgeReviewLog.operator_id == operator_id)
            .order_by(KnowledgeReviewLog.created_at.desc(), KnowledgeReviewLog.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_for_knowledge(self, knowledge_id: str) -> int:
        """某条目的变更条数，供测试与监控断言"每次成功迁移恰好加一条"。"""
        return len(await self.list_for_knowledge(knowledge_id))

    async def list_for_knowledge_ids(self, knowledge_ids: Sequence[str]) -> list[KnowledgeReviewLog]:
        """批量取多条的变更记录，按 (knowledge_id, created_at) 升序。"""
        if not knowledge_ids:
            return []
        stmt = (
            select(KnowledgeReviewLog)
            .where(KnowledgeReviewLog.knowledge_id.in_(list(knowledge_ids)))
            .order_by(KnowledgeReviewLog.knowledge_id, KnowledgeReviewLog.created_at, KnowledgeReviewLog.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


__all__ = ["KnowledgeReviewLogRepository"]
