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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import OwnershipError
from app.db.base import new_id
from app.models import PromptItem, PromptSnapshot
from app.repositories.base import OwnedRepository

# 历史快照的 `process_id` 可能为空（本列在 T3-15 才补出）。分组统计时归到该键下，使
# "取不到流程"的条目可见而非被静默丢弃，供复盘判断是否需要回填。
UNKNOWN_PROCESS = "unknown"


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

    async def get_item(self, db: AsyncSession, prompt_item_id: str) -> PromptItem | None:
        """按 `id` 取单条提示条目，供事件消费等无归属场景使用。

        `prompt_item_id` 全局唯一，故不带 `user_id` 过滤，属"显式孤立的无归属查询"
        （与 `list_items_by_followup_task` 同口径），调用方自行保证 `id` 来源可信。
        """
        stmt = select(PromptItem).where(PromptItem.id == prompt_item_id)
        return (await db.execute(stmt)).scalars().first()

    async def get_item_for_user(self, db: AsyncSession, user_id: str, item_id: str) -> PromptItem | None:
        """按归属取单条提示条目，供执行勾选回执使用（T3-15）。

        与 `get_item` 的差别只在是否带归属过滤：勾选是农户对自己条目的操作，必须走归属
        校验（T1-06），读不到他人的条目。归属不符返回 `None`，由服务层折算为 404，
        不区分"不存在"与"不属于你"（与基类 `get` 同口径）。
        """
        uid = self._require_user(user_id)
        stmt = select(PromptItem).where(PromptItem.id == item_id, PromptItem.user_id == uid)
        return (await db.execute(stmt)).scalars().first()

    async def save_item(self, db: AsyncSession, user_id: str, item: PromptItem) -> PromptItem:
        """保存单条提示条目的改动（不提交，由调用方 commit）。

        与 `add_item` 同口径地强制归属：`item.user_id` 缺失即抛 `MissingOwnerError`，
        与当前用户不一致即抛 `OwnershipError`。勾选回执改的是 `executed` / `executed_at`
        两列，调用方改完后交本方法落回会话。
        """
        uid = self._require_user(user_id)
        if getattr(item, "user_id", None) not in (None, uid):
            raise OwnershipError("保存对象的 user_id 与当前用户不一致")
        item.user_id = uid
        db.add(item)
        return item

    async def count_items(
        self,
        db: AsyncSession,
        since: date,
        process_id: str,
        *,
        suppressed: bool = False,
        executed: bool | None = None,
    ) -> int:
        """统计自 `since` 起、落在指定流程内的提示条目数（T3-15 执行率的分母/分子）。

        `process_id` 是必传的统计维度（产品说明书 6.3"按流程分别统计"），流程落在快照
        `prompt_snapshot.process_id` 上，故按 `snapshot_id` 内连接快照读取；`prompt_item`
        本身无流程列（T3-01 的列定义即如此）。`since` 与 `snapshot.snapshot_date` 同为
        `Date`，按日粒度过滤。

        `suppressed=False` 默认只统计展示过的条目：被抑制条目从未展示，计入分母会把执行率
        人为压低（11.3）。`executed` 为 `None` 时不加勾选条件（统计全部分母），为 `True` 时
        只数已勾选的条目（分子）。
        """
        stmt = (
            select(func.count())
            .select_from(PromptItem)
            .join(PromptSnapshot, PromptItem.snapshot_id == PromptSnapshot.id)
            .where(
                PromptSnapshot.snapshot_date >= since,
                PromptSnapshot.process_id == process_id,
                PromptItem.suppressed.is_(suppressed),
            )
        )
        if executed is not None:
            stmt = stmt.where(PromptItem.executed.is_(executed))
        return int((await db.execute(stmt)).scalar_one())

    async def count_items_by_process(
        self, db: AsyncSession, since: date, *, suppressed: bool = False, executed: bool | None = None
    ) -> dict[str, int]:
        """按流程分组统计提示条目数（T3-15，不传流程时执行率的分布口径）。

        与 `count_items` 同一套过滤，只是把 `process_id` 从过滤条件改成分组维度：一次查询
        取全部流程的计数，避免为七条流程各发一次查询。流程为 `NULL` 的历史快照归到
        `UNKNOWN_PROCESS` 之下，使其可见而非被静默丢弃。
        """
        stmt = (
            select(PromptSnapshot.process_id, func.count())
            .select_from(PromptItem)
            .join(PromptSnapshot, PromptItem.snapshot_id == PromptSnapshot.id)
            .where(PromptSnapshot.snapshot_date >= since, PromptItem.suppressed.is_(suppressed))
            .group_by(PromptSnapshot.process_id)
        )
        if executed is not None:
            stmt = stmt.where(PromptItem.executed.is_(executed))
        rows = (await db.execute(stmt)).all()
        return {(str(process_id) if process_id is not None else UNKNOWN_PROCESS): int(count) for process_id, count in rows}

    async def list_items_by_followup_task(self, db: AsyncSession, task_id: str) -> Sequence[PromptItem]:
        """按回访任务 id 取该任务生成的提示条目，供 T3-12 的 `followup.due` 消费者定位推送对象。

        判据是 `rule_id` 的后缀 `:{task_id}`（T3-04 的 `build_followup_rule` 把 `task_id` 追加到
        规则标识尾部，保证同一 `content_rule` 被多次复用时逐任务唯一）。这正是"推送内容只能来自
        已落库的 `prompt_item`"（11.8）在事件消费侧的落点：消费者拿到的 `followup.due` 载荷只有
        `task_id`，据它回查已落库条目，而不是用载荷里的字段另行拼装文案。

        不带 `user_id`：载荷本身不含 `user_id`，`task_id` 全局唯一，回查天然定位到唯一农户的
        条目，与 `get_item` 同属"显式孤立的无归属查询"。快照可能尚未生成（`followup.due` 与
        `snapshot.requested` 是两个互不阻塞的消费者），此时返回空序列，推送退化为无操作，由
        下一次触达扫描兜底。
        """
        if not task_id:
            return []
        stmt = select(PromptItem).where(PromptItem.rule_id.like(f"%:{task_id}"))
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

        **先 `flush` 快照再写条目**：`prompt_item.snapshot_id` 是指向 `prompt_snapshot.id`
        的外键，而两张表之间没有 ORM `relationship`，单次 flush 的插入顺序不受外键约束保证
        （`prompt_item` 可能先于 `prompt_snapshot` 落库而撞 `fk_prompt_item_snapshot`）。
        显式 flush 把父行先推下去，父行不在同一次 flush 里排队，顺序即确定。
        """
        uid = self._require_user(user_id)
        if not snapshot.id:
            snapshot.id = new_id("snap")
        await self.add(db, uid, snapshot)
        await db.flush()
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

__all__ = ["UNKNOWN_PROCESS", "SnapshotRepository", "snapshot_repo"]
