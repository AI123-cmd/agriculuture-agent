"""农事台账仓储（T3-15，产品说明书 4.5.2 功能点 2）。

台账是农户自己的私有农事记录，所有查询一律经 `OwnedRepository` 强制归属过滤（守全局约定
L4，禁止 `db.query`）。本仓储只提供三个入口：

1. `get_by_source`：按幂等键 `(user_id, source_type, source_id)` 查既有条目，入台账前用它
   判断是否重复点击。
2. `save`：写入一条台账条目，归属由基类 `add` 强制。
3. `list_by_date_desc`：按 `entry_date` 倒序取全部条目，供台账列表呈现（无生育期流程的
   纯时间线同样走这条）。

幂等键的唯一性由 `ledger_entry` 上的 `uq_ledger_entry_source` 约束兜底：并发两次点击即使
都通过了 `get_by_source` 的检查，第二次插入也会撞唯一约束，由服务层的 `unit_of_work`
折算为 409，不会落下重复记录。
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LedgerEntry
from app.repositories.base import OwnedRepository


class LedgerRepository(OwnedRepository[LedgerEntry]):
    """农事台账仓储。缺 `user_id` 抛 `MissingOwnerError`，归属不符读到 `None` / 影响 0 行。"""

    def __init__(self) -> None:
        super().__init__(LedgerEntry)

    async def get_by_source(
        self, db: AsyncSession, user_id: str, source_type: str, source_id: str
    ) -> LedgerEntry | None:
        """按幂等键取既有台账条目，取不到返回 `None`。

        三个条件都在此固定，调用方无法漏传 `user_id`：入台账的幂等判定必须与唯一约束
        `(user_id, source_type, source_id)` 同口径，否则"查不到 → 再插入"会与约束冲突。
        """
        return await self.first(db, user_id, source_type=source_type, source_id=source_id)

    async def save(self, db: AsyncSession, entry: LedgerEntry) -> LedgerEntry:
        """写入一条台账条目（不提交，由调用方 commit）。

        归属由基类 `add` 强制：`entry.user_id` 缺失即抛 `MissingOwnerError`，调用方无法
        落一条无归属的台账。`id` 已在服务层用 `new_id("lg")` 生成，此处不再补。
        """
        return await self.add(db, entry.user_id, entry)

    async def list_by_date_desc(
        self, db: AsyncSession, user_id: str, limit: int = 50, offset: int = 0
    ) -> Sequence[LedgerEntry]:
        """按 `entry_date` 倒序取台账条目，供台账列表呈现（无生育期流程的纯时间线亦然）。"""
        return await self.list(db, user_id, limit=limit, offset=offset, order_by=LedgerEntry.entry_date.desc())


ledger_repo = LedgerRepository()

__all__ = ["LedgerRepository", "ledger_repo"]
