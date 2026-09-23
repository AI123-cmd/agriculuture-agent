"""农事播报仓储。"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BroadcastLog
from app.repositories.base import OwnedRepository


class BroadcastLogRepository(OwnedRepository[BroadcastLog]):
    def __init__(self) -> None:
        super().__init__(BroadcastLog)

    async def list_by_date(self, db: AsyncSession, user_id: str, day: str) -> Sequence[BroadcastLog]:
        """取某日播报，按时间升序（展示顺序）。"""
        return await self.list_all(db, user_id, order_by=BroadcastLog.time, date=day)

    async def list_for_user(self, db: AsyncSession, user_id: str) -> Sequence[BroadcastLog]:
        return await self.list_all(db, user_id)

    async def mark_done(self, db: AsyncSession, user_id: str, item_id: str) -> int:
        """标记完成。归属不符时 `update_fields` 影响 0 行，不抛异常。

        这里刻意不抛 403：前端可能在同一条播报上重复点击，重复标记不是错误。
        """
        return await self.update_fields(db, user_id, item_id, done=1)


broadcast_repo = BroadcastLogRepository()

__all__ = ["BroadcastLogRepository", "broadcast_repo"]
