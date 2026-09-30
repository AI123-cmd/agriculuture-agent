"""提示快照仓储（T3-08，技术方案 11.4 / 12.5）。

`prompt_snapshot` 是"一次提示生成"的结果容器，`prompt_item` 是其内的条目。本仓储只服务
快照生成链路：按幂等键取当天快照、把新生成的快照与其条目一次写入、同日再次生成时自增
`revision`、档案变更后把旧快照标记失效。

三个设计取舍：

1. **只走 ORM**：查询一律用 `select` / `update` 表达式构造，不出现 `db.query`（全局约定 L4）。
   归属过滤经基类的 `_owned_query` 与 `_require_user` 完成，调用方无法绕过。
2. **`bump_revision` 在库侧自增**：`revision = revision + 1` 写在 UPDATE 语句里，而不是
   "先读当前值、Python 加一再写回"。后者在并发点击下会把两次更新压成同一个版本号，
   与 `(user_id, farm_entry_id, snapshot_date, revision)` 唯一约束的初衷相悖。
3. **`save` 级联写入条目**：`prompt_item` 经 `snapshot_id` 归属快照，本仓储在写入时统一补
   `snapshot_id` 与 `user_id`，调用方只负责给出条目内容，避免每条都手写这两个归属字段。

`get_today` 的 `farm_entry_id` 允许为空：杂草流程不绑定地块（12.2），此时按
`farm_entry_id IS NULL` 过滤，与迁移里那条部分唯一索引 `uq_prompt_snapshot_daily_no_farm`
的口径一致。
"""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import OwnershipError
from app.db.base import new_id
from app.models import PromptItem, PromptSnapshot
from app.repositories.base import OwnedRepository


class SnapshotRepository(OwnedRepository[PromptSnapshot]):
    """提示快照仓储。缺 `user_id` 抛 `MissingOwnerError`，归属不符读到 `None` / 影响 0 行。"""

    def __init__(self) -> None:
        super().__init__(PromptSnapshot)

    async def get_today(
        self, db: AsyncSession, user_id: str, farm_entry_id: str | None, today: date
    ) -> PromptSnapshot | None:
        """取当天该农户（该地块）的有效快照，多条时取 `revision` 最大的一条。

        `invalidated=True` 的旧快照留在库里做审计，但不再作为幂等命中返回——否则农户在
        档案变更后重新生成时会永远拿到那份已经不对的旧内容（11.4 的 `invalidated` 口子）。

        `farm_entry_id` 为 `None` 时按 `farm_entry_id IS NULL` 过滤，而不是"忽略该条件"：
        后者会让杂草流程（不绑定地块）误命中带地块的快照，两个流程的提示会串台。
        """
        uid = self._require_user(user_id)
        stmt = self._owned_query(uid).where(
            PromptSnapshot.snapshot_date == today,
            PromptSnapshot.invalidated.is_(False),
        )
        if farm_entry_id is None:
            stmt = stmt.where(PromptSnapshot.farm_entry_id.is_(None))
        else:
            stmt = stmt.where(PromptSnapshot.farm_entry_id == farm_entry_id)
        stmt = stmt.order_by(PromptSnapshot.revision.desc())
        return (await db.execute(stmt)).scalars().first()

    async def list_items(self, db: AsyncSession, user_id: str, snapshot_id: str) -> Sequence[PromptItem]:
        """取某份快照内的全部条目（含被压制项），按归属过滤。

        合并去重（T3-08 第二节）需要"快照已含哪些 `rule_id`"，故这里也要把
        `suppressed=True` 的条目一并取出——否则被压制过的信号会在下次点击时被
        当成新信号重复合并进来，同日条目数凭空增长。
        """
        uid = self._require_user(user_id)
        stmt = select(PromptItem).where(
            PromptItem.user_id == uid,
            PromptItem.snapshot_id == snapshot_id,
        )
        return (await db.execute(stmt)).scalars().all()

    async def add_item(self, db: AsyncSession, user_id: str, item: PromptItem) -> PromptItem:
        """向已存在的快照追加一条条目（不提交，由调用方 commit）。

        归属由本方法强制写入，与基类 `add` 同一口径；单列出来是因为基类的 `add`
        类型上绑定 `PromptSnapshot`，而快照生成链路要写入的是 `PromptItem`。
        """
        uid = self._require_user(user_id)
        if getattr(item, "user_id", None) not in (None, uid):
            raise OwnershipError("新增对象的 user_id 与当前用户不一致")
        item.user_id = uid
        db.add(item)
        return item

    async def save(self, db: AsyncSession, user_id: str, snapshot: PromptSnapshot) -> PromptSnapshot:
        """写入快照并级联写入其 `items`（不提交，由调用方 commit）。

        条目通过对象上的 `items` 属性传入；两者都缺主键时在此处补 `new_id`，使调用方可以
        只关心内容。`snapshot_id` 与 `user_id` 由仓储统一填，条目无法挂到别人的快照上。
        """
        uid = self._require_user(user_id)
        if not snapshot.id:
            snapshot.id = new_id("snap")
        await self.add(db, uid, snapshot)
        for item in getattr(snapshot, "items", ()) or ():
            if not item.id:
                item.id = new_id("item")
            item.snapshot_id = snapshot.id
            await self.add(db, uid, item)
        return snapshot

    async def bump_revision(self, db: AsyncSession, user_id: str, snapshot_id: str) -> int:
        """把快照的 `revision` 自增 1 并返回新版本号；未命中（不存在或归属不符）返回 0。

        `revision + 1` 由数据库求值（模块注释第二条），返回新值使调用方不必再查一次就能拿到
        "这是今日第几次更新"。有效版本号从 1 起，故 0 只可能表示"没有这一行"。
        """
        uid = self._require_user(user_id)
        stmt = (
            update(PromptSnapshot)
            .where(PromptSnapshot.id == snapshot_id, PromptSnapshot.user_id == uid)
            .values(revision=PromptSnapshot.revision + 1)
            .returning(PromptSnapshot.revision)
        )
        result = await db.execute(stmt)
        new_revision = result.scalar_one_or_none()
        return int(new_revision) if new_revision is not None else 0

    async def mark_invalidated(self, db: AsyncSession, user_id: str, snapshot_id: str) -> int:
        """把快照标为逻辑失效（不物理删除），返回受影响行数。

        置位而非删除：快照与条目是执行率统计与产品复盘的原始数据（11.3），物理删除会污染
        历史口径。归属过滤由基类 `update_fields` 施加，他人快照影响 0 行。
        """
        return await self.update_fields(db, user_id, snapshot_id, invalidated=True)


snapshot_repo = SnapshotRepository()

__all__ = ["SnapshotRepository", "snapshot_repo"]
