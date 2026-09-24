"""事务性发件箱：`emit` 与独立投递进程（技术方案 6.6）。

两件事放在同一模块是因为它们是同一协议的两端，必须成对理解：

- `emit` 在**业务事务内**只 `add` 一行，不提交。业务回滚则事件一并消失，
  不会出现"有事件无数据"；业务成功则事件随业务提交一起落库。
- `OutboxDispatcher` 在**另一个进程**里把已落库的事件投入 ARQ 队列。

为什么不用"事务提交后直接 `enqueue`"：提交与入队之间失败（进程崩溃、Redis 抖动、
网络超时）事件就丢了，且业务方无法感知。发件箱把"写事件"与"业务写"放进同一个
数据库事务，投递失败只表现为该行的 `status`，可以重试、可以查、可以人工介入。

投递延迟上限为一轮 cron 间隔（5 秒），对回访与触达足够。**对延迟敏感的
`snapshot.requested`（农户点「更新提示」后正等结果）不走发件箱**，走同步路径
直接生成——发件箱服务于"可以等 5 秒"的事件，不服务于"用户正在等结果"的请求。
"""

import logging
import uuid
from datetime import datetime, timedelta

from arq.connections import ArqRedis, RedisSettings, create_pool
from sqlalchemy import ColumnElement, Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.events.handlers import EVENT_TYPES, resolve_task
from app.models.event_outbox import (
    STATUS_DEAD,
    STATUS_DISPATCHED,
    STATUS_PENDING,
    EventOutbox,
)

logger = logging.getLogger(__name__)

# 投递尝试上限：达到即转 `dead`，不再自动重试，等人工处理。
# 与 `BACKOFF_SECONDS` 的长度耦合：退避项的索引由 `attempts` 决定，两者应同步调整。
MAX_ATTEMPTS = 5

# 固定退避表（秒）。用表而非纯指数公式：公式在多次重试后会溢出到不可能的时间，
# 固定表让重试节奏可预测、可调参、可写进运维文档。
# 索引与 `attempts` 直接对应：第 1 次失败用 30 秒，第 2 次用 120 秒，依此类推。
BACKOFF_SECONDS = (5, 30, 120, 600, 1800)

# 抢占租约（秒）：抢占后该行在这么久内不再被其他 Dispatcher 抢走。
# 它同时是崩溃恢复机制——进程在投递前死掉，该行仍是 `pending`，租约到期后
# 自动回到待投递集合（"至少一次投递 + 消费者幂等"）。取 60 秒是因为投递本身
# 只是"入队"，正常情况下远快于此。
CLAIM_LEASE_SECONDS = 60

# `last_error` 的截断长度：留够定位信息即可，避免超长堆栈把行撑大。
_MAX_ERROR_CHARS = 1000


def _future(seconds: int) -> ColumnElement[datetime]:
    """`now() + N 秒` 的 SQL 表达式。

    在库侧求值而非用应用侧 `datetime.now(UTC)`：应用与数据库时钟若有偏差，
    未来时刻会被算错，而 `available_at <= now()` 的比较是在库侧做的，
    两边对"现在"的理解必须一致。
    """
    return func.now() + timedelta(seconds=seconds)


async def emit(
    db: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    user_id: str | None,
    payload: dict,
    idempotency_key: str | None = None,
) -> EventOutbox:
    """在当前事务内写入待投递事件；不 commit，提交由 `unit_of_work` 负责。

    这是"事件与业务同生共死"的实现：业务失败则事务回滚、事件一并消失，不会有
    "有事件无数据"；业务成功则事件随提交落库，不会有"有数据无事件"。

    刻意不 `flush`：`flush` 会把 INSERT 提前推给数据库，回滚虽然也能撤销它，
    但白消耗一次往返。事件随业务提交落库才是本模式的意义，`add` 已经足够。

    `event_type` 不在注册表内时记一条告警：投递端有兜底任务不会丢事件，但写错
    事件名的代价应当在产生它的那一刻就可见，而不是等到翻投递日志。
    """
    if event_type not in EVENT_TYPES:
        logger.warning("未注册的事件类型 %s，投递时将走兜底任务", event_type)

    row = EventOutbox(
        id=uuid.uuid4().hex,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        user_id=user_id,
        payload=payload,
        idempotency_key=idempotency_key or f"{event_type}:{aggregate_id}",
        status=STATUS_PENDING,
    )
    db.add(row)
    return row


async def create_outbox_pool() -> ArqRedis:
    """按配置建一个 ARQ 连接池（默认队列）。

    正常情况下 worker 会把自己已有的连接池传给 `dispatch_once`（见 `app/worker.py`），
    这个入口是给手工触发与脚本用的。`REDIS_URL` 缺失时直接抛错而不退化到
    localhost：静默连到本机 Redis 会让"生产忘了配"表现为"事件永不出队"，
    是最难查的一类故障。
    """
    url = get_settings().REDIS_URL
    if not url:
        raise RuntimeError("REDIS_URL 未配置，发件箱无法投递（T1-08 的生产启动自检会拦住这种部署）")
    return await create_pool(RedisSettings.from_dsn(url))


class OutboxDispatcher:
    """独立进程：批量抢占待投递事件，投入 ARQ 队列，回写投递结果。

    与任务骨架的一处关键偏差（必要）：骨架的 `_claim` 只加 `FOR UPDATE` 锁，
    提交抢占后 `status` 仍是 `pending`，于是第二个 Dispatcher 会重新抢到同一批行、
    把同一条事件投递两次，`attempts` 变成 2——直接违反"两个 Dispatcher 并发处理
    200 条时每条 `attempts` 恰好为 1"这条验收标准。

    本实现把"抢占"实现为一条**自增 `attempts` 并推后 `available_at`** 的
    `UPDATE`（见 `claim_stmt`）：抢占动作本身就把该行标记成"已被领走"，在租约内
    不可能被第二次抢到。两个 Dispatcher 靠 `FOR UPDATE SKIP LOCKED` 拿到互不相交
    的行，各自已在同一事务里把 `available_at` 推到租约之后，因此不会重复。

    `attempts` 在抢占时自增，于是它的语义就是"实际投递尝试次数"，与退避表的
    索引天然对齐，不需要在投递失败时再补一次自增。
    """

    def __init__(
        self,
        batch_size: int | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        # 缺省从配置读，而不是写死 100：运维要能只改环境变量就调整单轮批量。
        self.batch_size = batch_size or get_settings().OUTBOX_BATCH_SIZE
        # 可注入会话工厂：测试用 NullPool 引擎自建工厂，避免跨事件循环复用连接池。
        self._session_factory = session_factory or SessionLocal

    def claim_stmt(self, limit: int) -> Update:
        """抢占语句（不执行，供单测核对 SQL 形态）。三部分缺一不可：

        1. 内层 `SELECT ... ORDER BY available_at LIMIT n FOR UPDATE SKIP LOCKED`
           取最该被投递的 n 行并加行锁。`SKIP LOCKED` 让第二个 Dispatcher 跳过
           别人已锁的行去取后面的，既不阻塞也不重复——普通 `FOR UPDATE` 会让后者
           干等，完全不锁则必然重复投递。
        2. `attempts + 1`：抢占即计一次尝试。放在这里而不是投递之后，是为了让
           "已领走"与"尝试次数"同时落库，中途崩溃也不会把同一次尝试算两遍。
        3. `available_at = now() + 租约`：抢占期间该行不可再被抢，同时提供崩溃
           恢复；失败时会被退避表算出的时刻覆盖（见 `_record_failure`）。

        外层用 `UPDATE ... WHERE id IN (子查询)` 而非"先 SELECT 再逐行 UPDATE"：
        一条语句里完成"挑行 + 加锁 + 标记"，不存在两次往返之间的竞态窗口。

        `RETURNING` 直接回读被抢占的行，省掉一次回表查询；
        `synchronize_session=False` 是因为结果已由 `RETURNING` 给出。
        """
        claimable = (
            select(EventOutbox.id)
            .where(EventOutbox.status == STATUS_PENDING, EventOutbox.available_at <= func.now())
            .order_by(EventOutbox.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return (
            update(EventOutbox)
            .where(EventOutbox.id.in_(claimable))
            .values(
                attempts=EventOutbox.attempts + 1,
                available_at=_future(CLAIM_LEASE_SECONDS),
            )
            .returning(EventOutbox)
            .execution_options(synchronize_session=False)
        )

    async def dispatch_once(self, pool: ArqRedis | None = None) -> int:
        """跑一轮投递，返回本轮处理条数。

        `pool` 可注入：worker 侧传入自己的连接池（复用连接），测试侧注入桩对象
        以便让投递「必然失败」来验证退避与死信。缺省时自建临时池并在结束时关闭。
        """
        client = pool
        owns_pool = False
        if client is None:
            client = await create_outbox_pool()
            owns_pool = True
        try:
            async with self._session_factory() as db:
                rows = await self._claim(db)
                if not rows:
                    return 0
                # 抢占后立即提交：释放行锁、缩短持锁时间，让后续 cron 轮次与人工
                # 排查不被阻塞。空窗期（已提交抢占、尚未投递）由租约覆盖。
                await db.commit()
                for row in rows:
                    await self._deliver(db, client, row)
                return len(rows)
        finally:
            if owns_pool:
                await client.aclose()

    async def _claim(self, db: AsyncSession) -> list[EventOutbox]:
        result = await db.execute(self.claim_stmt(self.batch_size))
        return list(result.scalars().all())

    async def _deliver(self, db: AsyncSession, pool: ArqRedis, row: EventOutbox) -> None:
        """投递单条事件并回写结果。失败不向上抛：一条事件失败不该中断整批。"""
        try:
            await pool.enqueue_job(resolve_task(row.event_type), row.payload, event_id=row.id)
        except Exception as exc:  # noqa: BLE001  失败原因不决定分支，一律按可重试处理
            await self._record_failure(db, row, exc)
            return

        await db.execute(
            update(EventOutbox)
            .where(EventOutbox.id == row.id)
            .values(status=STATUS_DISPATCHED, dispatched_at=func.now())
            # `last_error` 不清空：一条重试后才成功的事件，它此前的失败原因正是
            # 排查同类问题的线索，清掉等于抹去现场。
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    async def _record_failure(self, db: AsyncSession, row: EventOutbox, exc: Exception) -> None:
        """记录失败：未达上限则按退避表推后重试，达上限则转 `dead`。

        失败不做分类：`last_error` 保留原文供死信排查，真正的分类在 T3-14 的
        `reach_record.fail_reason` 做——那时才有"是投递问题还是内容问题"的语境。
        """
        attempts = row.attempts  # 抢占时已自增，见 claim_stmt
        values: dict = {"last_error": f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]}
        if attempts >= MAX_ATTEMPTS:
            values["status"] = STATUS_DEAD
        else:
            values["status"] = STATUS_PENDING
            # 覆盖抢占时写的租约时刻：已知失败后，退避表才是权威的调度依据。
            # 退避项按 attempts 取，越界时取最后一项（attempts >= MAX_ATTEMPTS
            # 已在此前的分支返回，故不会越界）。
            values["available_at"] = _future(BACKOFF_SECONDS[attempts])

        await db.execute(
            update(EventOutbox)
            .where(EventOutbox.id == row.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        logger.warning(
            "事件投递失败 event=%s type=%s attempts=%s status=%s",
            row.id,
            row.event_type,
            attempts,
            values["status"],
        )


__all__ = [
    "BACKOFF_SECONDS",
    "CLAIM_LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "OutboxDispatcher",
    "create_outbox_pool",
    "emit",
]
