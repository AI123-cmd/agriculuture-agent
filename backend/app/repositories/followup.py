"""回访任务仓储（T3-04 读取入口 + T3-09 登记入口，技术方案 11.2 / 12.4）。

T3-09（回访调度服务）负责登记回访任务（`register_from_prescription`），本项提供：
T3-04 读取到期任务所需的 `list_due`、回填关闭任务所需的 `mark_done` / `list_by_user`，
以及 T3-09 的写入入口 `bulk_save` 与每日扫描入口 `list_due_all`。

`due_at` 用绝对时间戳存（契约层是相对的 `due_offset_days`），因此到期扫描是纯时间范围
查询，走 `ix_followup_task_status_due` 复合索引。

**`list_due_all` 是唯一不带 `user_id` 的查询**：它服务 worker 的每日到期扫描，是全库
范围而非某个农户的查询，因此无法按归属过滤。归属仍由基类保证其余全部方法，孤立这一条
是为了让"唯一的例外"显式可见，而不是散落成多处无归属过滤的裸查询。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import FollowupTask
from app.models.enums import FollowupStatus
from app.repositories.base import OwnedRepository


class FollowupRepository(OwnedRepository[FollowupTask]):
    def __init__(self) -> None:
        super().__init__(FollowupTask)

    async def bulk_save(self, db: AsyncSession, tasks: Sequence[FollowupTask]) -> Sequence[FollowupTask]:
        """批量登记回访任务（不提交，由调用方 commit）。

        `prescription.created` 的消费者一次登记一条处方卡的全部回访任务，逐条 `add`
        让 SQLAlchemy 在同一事务里排队 INSERT。`user_id` 由基类 `add` 强制校验：
        缺失（`None` / 空串）即抛 `MissingOwnerError`，调用方无法登记无归属的任务。

        与 `snapshot_repo.save` 一致地补 `new_id`：调用方只关心内容时也能落库。此处
        不做 `flush`——登记时引用的 `consultation` / `prescription_card` 由反查得到，
        是已落库的行，不存在同一次 flush 内的外键顺序问题。
        """
        saved: list[FollowupTask] = []
        for task in tasks:
            if not task.id:
                task.id = new_id("fu")
            await self.add(db, task.user_id, task)
            saved.append(task)
        return saved

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

    async def list_due_all(self, db: AsyncSession, today: date) -> Sequence[FollowupTask]:
        """全库到期未回访的任务：`status='pending'` 且 `due_at <= 今天结束`。

        这是本仓储唯一不带 `user_id` 的查询（见模块注释）：它服务 worker 的每日到期
        扫描，要横跨全部农户挑出该触达的任务，再按人分派 `followup.due` 事件。判定口径
        与 `list_due` 完全一致（到"当天结束"而非"此刻"，理由同上），差异只在是否按归属
        过滤。走 `ix_followup_task_status_due` 复合索引。
        """
        end_of_day = datetime.combine(today, time.max)
        stmt = (
            select(FollowupTask)
            .where(FollowupTask.status == FollowupStatus.PENDING.value, FollowupTask.due_at <= end_of_day)
            .order_by(FollowupTask.due_at)
        )
        return (await db.execute(stmt)).scalars().all()

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
