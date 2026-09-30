"""回访任务仓储（T3-04，技术方案 11.2 / 12.4）。

T3-09（回访调度服务）负责登记回访任务（`register_from_prescription`），本项只提供
T3-04 读取到期任务所需的 `list_due`，以及回填关闭任务所需的 `mark_done` / `list_by_user`。

`due_at` 用绝对时间戳存（契约层是相对的 `due_offset_days`），因此到期扫描是纯时间范围
查询，走 `ix_followup_task_status_due` 复合索引。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FollowupTask
from app.models.enums import FollowupStatus
from app.repositories.base import OwnedRepository


class FollowupRepository(OwnedRepository[FollowupTask]):
    def __init__(self) -> None:
        super().__init__(FollowupTask)

    async def list_due(self, db: AsyncSession, user_id: str, today: date) -> Sequence[FollowupTask]:
        """到期未回访的任务：`status='pending'` 且 `due_at <= 今天结束`。

        判定到"当天结束"而不是"此刻"：回访时点是按天计的，`due_at` 落在当日任一时刻
        都应视为当天到期，否则早上生成的当日任务会因 `due_at` 在下午而漏掉。
        """
        uid = self._require_user(user_id)
        end_of_day = datetime.combine(today, time.max)
        stmt = (
            self._owned_query(uid)
            .where(FollowupTask.status == FollowupStatus.PENDING.value, FollowupTask.due_at <= end_of_day)
            .order_by(FollowupTask.due_at)
        )
        return (await db.execute(stmt)).scalars().all()

    async def list_by_user(self, db: AsyncSession, user_id: str) -> Sequence[FollowupTask]:
        """某用户全部回访任务，按到期时点升序（回访管理页用）。"""
        return await self.list_all(db, user_id, order_by=FollowupTask.due_at)

    async def mark_done(self, db: AsyncSession, user_id: str, task_id: str, *, result: str, note: str | None) -> int:
        """回填结果并关闭任务：置 `done`、写结果、记关闭时点。

        `closed_at` 由调用方（服务的当前时钟）显式给出，便于测试注入固定时点；
        此处用 `datetime.now()` 会引入不可控的时间源。
        """
        return await self.update_fields(
            db,
            user_id,
            task_id,
            status=FollowupStatus.DONE.value,
            result=result,
            result_note=note,
            closed_at=datetime.now(UTC),
        )


followup_repo = FollowupRepository()

__all__ = ["FollowupRepository", "followup_repo"]
