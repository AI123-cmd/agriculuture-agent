"""记忆摘要仓储：同一会话复用一条记录。"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import MemorySummary
from app.repositories.base import OwnedRepository


class MemorySummaryRepository(OwnedRepository[MemorySummary]):
    def __init__(self) -> None:
        super().__init__(MemorySummary)

    async def list_by_update_desc(self, db: AsyncSession, user_id: str) -> Sequence[MemorySummary]:
        """按更新时间倒序取全部摘要：`recall` 要取最近一条做中期记忆。"""
        return await self.list_all(db, user_id, order_by=MemorySummary.update_time.desc())

    async def list_for_user(self, db: AsyncSession, user_id: str) -> Sequence[MemorySummary]:
        return await self.list_all(db, user_id)

    async def get_by_session(self, db: AsyncSession, user_id: str, session_id: str) -> MemorySummary | None:
        return await self.first(db, user_id, session_id=session_id)

    async def upsert(
        self,
        db: AsyncSession,
        user_id: str,
        session_id: str,
        summary: str,
        key_facts: list[str],
    ) -> MemorySummary:
        """同一会话复用一条记录：已有则覆盖摘要并合并事实，没有则新建。

        不提交，由调用方（`ai_service._persist` 所在事务）统一提交。
        """
        existing = await self.get_by_session(db, user_id, session_id)
        if existing is not None:
            existing.summary = summary
            existing.key_facts = sorted({*(existing.key_facts or []), *key_facts})
            return existing
        row = MemorySummary(id=new_id("mem"), session_id=session_id, summary=summary, key_facts=key_facts)
        await self.add(db, user_id, row)
        return row


memory_repo = MemorySummaryRepository()

__all__ = ["MemorySummaryRepository", "memory_repo"]
