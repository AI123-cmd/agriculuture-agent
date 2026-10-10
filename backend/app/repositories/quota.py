"""订阅额度仓储（T3-11，技术方案 11.7 / 12.5）。

`subscription_quota` 是"一户一行"的额度账本，`user_id` 直接作主键、**没有 `id` 列**，
因此进不了泛型 `OwnedRepository`（基类的 `get` / `update_fields` / `delete` 都以
`self.model.id` 为键）。归属改由 `require_user_id` 强制（与 `message.py` 同一做法），
本仓储不提供任何返回裸语句或跳过归属校验的出口。

两条写入都不是"先查再改"，而是带条件的单条 SQL：

1. `try_consume`：`UPDATE ... WHERE remaining > 0 ... RETURNING`。判断与扣减在一条语句内
   完成。若拆成"先读 `remaining`、再判断、再写回"，并发推送会双花同一份额度——两个请求都
   读到 `remaining=1`、都判定可发，最终发出两条而额度只扣一次（11.7 第一条的落地）。
2. `grant` / `record_reject`：用 PostgreSQL 的 `INSERT ... ON CONFLICT DO UPDATE` 做原子
   自增/清零。授权首次到达时表里可能还没有该户的行，upsert 一步到位；并发首授也不会撞主键
   （`INSERT` 被 `ON CONFLICT` 吸收为一次 `UPDATE`），不需要"先 SELECT 再 INSERT"的竞态窗口。

`refund` 与 `try_consume` 对称，并以 `consumed_total > 0` 为前置：没有可退的消费记录时是
无操作，绝不把 `remaining` 凭空加上去或减成负数。

扣减/退还由调用方 `commit`（`quota_service` 在返回前提交），理由是"先扣额度再发送"的
顺序要求扣减在调用外部接口前已经落库。
"""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SubscriptionQuota
from app.repositories.base import execute_write, require_user_id


class QuotaRepository:
    """额度账本仓储。缺 `user_id` 抛 `MissingOwnerError`，归属不符读到 `None` / 影响 0 行。"""

    model = SubscriptionQuota

    async def get(self, db: AsyncSession, user_id: str) -> SubscriptionQuota | None:
        """取某户的额度行；从未有过交互则返回 `None`（视同全零账本，由服务层兜底）。"""
        uid = require_user_id("SubscriptionQuota", user_id)
        stmt = select(SubscriptionQuota).where(SubscriptionQuota.user_id == uid)
        return (await db.execute(stmt)).scalars().first()

    async def grant(self, db: AsyncSession, user_id: str) -> SubscriptionQuota:
        """记一次授权成功：`granted_total` 与 `remaining` 各加一、`reject_streak` 归零。

        upsert 而非 update：首授时表中无该户行，纯 `UPDATE` 会静默影响 0 行、授权凭空丢失。
        返回更新后的行供接口组装额度视图。
        """
        uid = require_user_id("SubscriptionQuota", user_id)
        now = func.now()
        stmt = (
            pg_insert(SubscriptionQuota)
            .values(user_id=uid, granted_total=1, consumed_total=0, remaining=1, reject_streak=0, last_grant_at=now)
            .on_conflict_do_update(
                index_elements=[SubscriptionQuota.user_id],
                set_={
                    "granted_total": SubscriptionQuota.granted_total + 1,
                    "remaining": SubscriptionQuota.remaining + 1,
                    "reject_streak": 0,
                    "last_grant_at": now,
                },
            )
            .returning(SubscriptionQuota)
        )
        return (await db.execute(stmt)).scalars().one()

    async def record_reject(self, db: AsyncSession, user_id: str) -> SubscriptionQuota:
        """记一次拒绝（含"沉默"）：`reject_streak` 加一，其余计数不动。

        同样用 upsert：农户可能先拒绝、后授权（或始终不授权），拒绝时表里未必有行。首行按
        `reject_streak=1`、其余计数为 0 建立。连续三天无额度且无新增授权也走这里加一
        （由调度任务按日调用），这样"不点任何按钮"的沉默也被计入，避免系统误以为还有机会。
        """
        uid = require_user_id("SubscriptionQuota", user_id)
        stmt = (
            pg_insert(SubscriptionQuota)
            .values(user_id=uid, granted_total=0, consumed_total=0, remaining=0, reject_streak=1)
            .on_conflict_do_update(
                index_elements=[SubscriptionQuota.user_id],
                set_={"reject_streak": SubscriptionQuota.reject_streak + 1},
            )
            .returning(SubscriptionQuota)
        )
        return (await db.execute(stmt)).scalars().one()

    async def try_consume(self, db: AsyncSession, user_id: str) -> bool:
        """原子扣减一份额度，返回是否成功；失败即额度不足，调用方不得发送。

        `WHERE remaining > 0` 使扣减具备条件性，`RETURNING` 让"是否命中"由同一条语句给出。
        返回 `False` 只可能表示"无该户行"或"额度已耗尽"——两者对调用方的含义相同：不可发。
        """
        uid = require_user_id("SubscriptionQuota", user_id)
        stmt = (
            update(SubscriptionQuota)
            .where(SubscriptionQuota.user_id == uid, SubscriptionQuota.remaining > 0)
            .values(
                remaining=SubscriptionQuota.remaining - 1,
                consumed_total=SubscriptionQuota.consumed_total + 1,
                last_consume_at=func.now(),
            )
            .returning(SubscriptionQuota.user_id)
        )
        return (await db.execute(stmt)).first() is not None

    async def refund(self, db: AsyncSession, user_id: str) -> bool:
        """退还一份额度（与 `try_consume` 对称），返回是否成功。

        前置 `consumed_total > 0`：只有在确有"已消费但未成功送达"的记录时才退，否则是无操作。
        没有这条前置，退还会凭空增加 `remaining`（额度越退越多）；若反过来写成对 `remaining`
        做减法，则会把 `remaining` 减成负数。两种错误都由这条前置挡住。
        """
        uid = require_user_id("SubscriptionQuota", user_id)
        stmt = (
            update(SubscriptionQuota)
            .where(SubscriptionQuota.user_id == uid, SubscriptionQuota.consumed_total > 0)
            .values(
                remaining=SubscriptionQuota.remaining + 1,
                consumed_total=SubscriptionQuota.consumed_total - 1,
            )
            .returning(SubscriptionQuota.user_id)
        )
        return (await db.execute(stmt)).first() is not None

    async def zero_out(self, db: AsyncSession, user_id: str) -> int:
        """把某户剩余额度清零，返回受影响行数（T3-12 的 `43101` 分支调用）。

        触发场景：农户在授权后又关闭了订阅，微信返回 `43101`。此时系统持有的每一份授权都
        已失效，`remaining` 若不归零会让下次扫描继续拿着失效授权反复失败。`granted_total`
        与 `consumed_total` 不动——它们是历史事实，不应被追溯修改。
        """
        uid = require_user_id("SubscriptionQuota", user_id)
        stmt = (
            update(SubscriptionQuota)
            .where(SubscriptionQuota.user_id == uid)
            .values(remaining=0)
        )
        return await execute_write(db, stmt)


quota_repo = QuotaRepository()

__all__ = ["QuotaRepository", "quota_repo"]
