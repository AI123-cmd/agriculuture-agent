"""触达记录仓储（T3-14 + T3-12，技术方案 12.5 / 11.6）。

`reach_record` 是每一次触达尝试的原始记录，本仓储提供三组能力：

1. **写入**：`save` 落一条记录（`record_attempt` 的唯一出口）。
2. **统计**：`count_by_status` / `count_by_fail_reason` 供 T6-09 的线上指标采集。
3. **扫描**：`list_unpushed_due` 供 T3-12 的推送链路挑出"待推送"的提示条目。

两处刻意的设计：

- `count_by_status` / `count_by_fail_reason` / `list_unpushed_due` 是**跨农户查询**：指标是
  全局口径、推送扫描要横跨全部农户，无法按归属过滤，与 `FollowupRepository.list_due_all`
  同属"显式孤立的无归属查询"，其余方法一律走基类的归属过滤。
- `list_unpushed_due` 返回的是 `PromptItem`（而非 `ReachRecord`）：推送链路要的是"还没送达过
  的提示条目"，`reach_record` 只是它"已送达/已终结"的判据。判据用 `NOT EXISTS` 子查询表达：
  某条目只要存在一条 `subscribe_once` 且"已送达或已终结"的记录，就不再作为候选；`quiet_hours`
  与 `api_error` 是可重试的，故**不**进入终结集合，条目保持待推送状态下个窗口重试。

`stage` 之外的读取（`list` / `get`）由基类提供，本仓储不重复实现。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PromptItem, PromptSnapshot, ReachRecord
from app.models.enums import Channel, FailReason, ReachStatus
from app.repositories.base import OwnedRepository

# 终结性失败原因：一旦命中即不再作为推送候选（需农户重新授权才能再发）。
# `quiet_hours` / `api_error` 刻意不在其中——两者都是"可重试"，条目应保持待推送。
TERMINAL_FAIL_REASONS: tuple[str, ...] = (
    FailReason.NO_GRANT.value,
    FailReason.QUOTA_EXHAUSTED.value,
)


def _since_datetime(since: date) -> datetime:
    """把统计起点（自然日）落成当日零点的 UTC 时点，与 `attempted_at` 的比较口径一致。"""
    return datetime.combine(since, time.min, tzinfo=UTC)


class ReachRepository(OwnedRepository[ReachRecord]):
    """触达记录仓储。缺 `user_id` 抛 `MissingOwnerError`，归属不符读到 `None` / 影响 0 行。"""

    def __init__(self) -> None:
        super().__init__(ReachRecord)

    async def save(self, db: AsyncSession, record: ReachRecord) -> ReachRecord:
        """写入一条触达记录（不提交，由调用方 commit）。

        归属由基类 `add` 强制：`record.user_id` 缺失即抛 `MissingOwnerError`，调用方无法
        落一条无归属的记录。这是 T3-14 `record_attempt` 的唯一落库出口。
        """
        return await self.add(db, record.user_id, record)

    async def count_by_status(self, db: AsyncSession, status: str, since: date) -> int:
        """统计自 `since` 起某状态的触达条数（全局口径，跨农户）。

        走 `ix_reach_record_user_attempted` 的时间前缀不适用（本查询不含 user_id），
        但触达记录量级不大且指标查询是低频离线任务，全表按 `attempted_at` 过滤可接受。
        """
        stmt = (
            select(func.count())
            .select_from(ReachRecord)
            .where(ReachRecord.status == status, ReachRecord.attempted_at >= _since_datetime(since))
        )
        return int((await db.execute(stmt)).scalar_one())

    async def count_by_fail_reason(self, db: AsyncSession, since: date) -> dict[str, int]:
        """统计自 `since` 起各失败原因的条数（全局口径），按原因分组。

        只统计 `fail_reason` 非空的记录（成功记录不进失败构成）；返回的字典只含出现过的
        原因，调用方用 `dict.get(reason, 0)` 取用，避免为四个原因硬编码补齐零值。
        """
        stmt = (
            select(ReachRecord.fail_reason, func.count())
            .select_from(ReachRecord)
            .where(ReachRecord.attempted_at >= _since_datetime(since), ReachRecord.fail_reason.is_not(None))
            .group_by(ReachRecord.fail_reason)
        )
        rows = (await db.execute(stmt)).all()
        return {str(reason): int(count) for reason, count in rows if reason is not None}

    async def list_unpushed_due(
        self, db: AsyncSession, now: datetime, limit: int = 200
    ) -> Sequence[PromptItem]:
        """待推送的提示条目：到期快照内、未被压制、且尚未送达/终结的条目。

        判定四条：快照未失效、`snapshot_date <= 今天`（到期）、条目未被压制、条目既没有
        `delivered` 记录也没有终结性失败记录。可重试的失败（`quiet_hours` / `api_error`）
        不算"已终结"，条目会再次成为候选；`no_grant` / `quota_exhausted` 是终结态
        （需农户重新授权），不再重试。

        阻塞判据不限定 `channel`：降级路径（额度耗尽 / 未授权）按 T3-14 记 `in_app`，
        送达成功记 `subscribe_once`，两者都应阻止条目被反复重扫。

        排序用 `priority_score` 降序：额度与次数有限时优先推最要紧的条目。
        """
        today = now.date()
        blocked = select(ReachRecord.id).where(
            ReachRecord.prompt_item_id == PromptItem.id,
            or_(
                ReachRecord.status == ReachStatus.DELIVERED.value,
                ReachRecord.fail_reason.in_(TERMINAL_FAIL_REASONS),
            ),
        )
        stmt = (
            select(PromptItem)
            .join(PromptSnapshot, PromptItem.snapshot_id == PromptSnapshot.id)
            .where(
                PromptSnapshot.invalidated.is_(False),
                PromptSnapshot.snapshot_date <= today,
                PromptItem.suppressed.is_(False),
                ~exists(blocked),
            )
            .order_by(PromptItem.priority_score.desc())
            .limit(limit)
        )
        return (await db.execute(stmt)).scalars().all()


reach_repo = ReachRepository()

__all__ = ["TERMINAL_FAIL_REASONS", "ReachRepository", "reach_repo"]
