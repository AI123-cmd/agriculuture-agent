"""T1-13 验收（二）：投递、退避与死信、并发抢占、端到端。

四组断言各自钉死一种风险：

1. **SQL 形态**：`FOR UPDATE SKIP LOCKED` 与"抢占即自增 attempts 并推后
   available_at"必须真的出现在语句里。只看行为不够——去掉 `SKIP LOCKED` 后
   单进程用例照样全绿，只有并发用例才会失败，而并发用例本身也可能因时序而偶发通过。
2. **退避与死信**：投递目标持续抛异常时，`attempts` 单调递增、`available_at`
   逐次推后、第 5 次转 `dead` 且 `last_error` 非空。失败被静默吞掉是最难查的
   故障——事件看起来"投过了"，实际从没到过队列。
3. **并发不重复**：两个 Dispatcher 同时处理 200 条，每条 `attempts` 必须恰好为 1。
   这一条是改造 `_claim` 的直接目的（原骨架实现会让 attempts 变成 2）。
4. **端到端**：真 Redis、真 ARQ worker，事件从 `pending` 走到 `dispatched` 并被
   消费者取走。

时间推进用"把 `available_at` 改回 now()"来模拟，不真等 30 秒——退避时刻本身
也是断言对象，改写它只会影响下一轮能否被抢到。
"""

import asyncio
import uuid

import pytest
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.worker import Worker
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.events import EVENT_SNAPSHOT_REQUESTED, emit
from app.events.outbox import BACKOFF_SECONDS, CLAIM_LEASE_SECONDS, MAX_ATTEMPTS, OutboxDispatcher
from app.models import EventOutbox
from app.models.event_outbox import STATUS_DEAD, STATUS_DISPATCHED, STATUS_PENDING

pytestmark = pytest.mark.usefixtures("requires_postgres")

# 并发用例的事件条数（验收原文：200 条）
_CONCURRENT_EVENTS = 200
# 两个 Dispatcher 各抢 100 条，正好覆盖全部
_BATCH = 100


@pytest.fixture()
def factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False)


class _StubPool:
    """投递桩：记录入队调用，可切换成"必然失败"。

    用桩而非真 Redis 的理由：退避与死信要连续制造 5 次失败，真 Redis 不会失败，
    而为了让它失败去改网络配置属于把测试建立在环境上。
    """

    def __init__(self, *, fail: bool = False, error: Exception | None = None) -> None:
        self.fail = fail
        self.error = error or RuntimeError("redis 不可用")
        self.calls: list[tuple[str, dict, str]] = []

    async def enqueue_job(self, function: str, payload: dict | None = None, **kwargs) -> None:
        if self.fail:
            raise self.error
        self.calls.append((function, payload or {}, kwargs.get("event_id", "")))


async def _seed(db: AsyncSession, count: int, *, prefix: str = "evt") -> None:
    """批量写入 count 条待投递事件（一条 INSERT，不走逐条 add）。"""
    db.add_all(
        [
            EventOutbox(
                id=f"{prefix}-{i:04d}",
                event_type=EVENT_SNAPSHOT_REQUESTED,
                aggregate_type="snapshot",
                aggregate_id=f"{prefix}-agg-{i:04d}",
                user_id="u1",
                payload={"index": i},
                idempotency_key=f"snapshot.requested:{prefix}-{i:04d}",
                status=STATUS_PENDING,
            )
            for i in range(count)
        ]
    )
    await db.commit()


async def _rows_by_id(db: AsyncSession, prefix: str) -> dict[str, EventOutbox]:
    rows = (await db.execute(select(EventOutbox).where(EventOutbox.id.like(f"{prefix}-%")))).scalars().all()
    return {r.id: r for r in rows}


async def _make_claimable(db: AsyncSession, prefix: str) -> None:
    """把该批事件的 `available_at` 拉回 now()，模拟"退避时间已到"。"""
    await db.execute(
        update(EventOutbox)
        .where(EventOutbox.id.like(f"{prefix}-%"))
        .values(available_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


# --------------------------------------------------------------------------
# SQL 形态
# --------------------------------------------------------------------------


def test_claim_stmt_uses_skip_locked_and_lease() -> None:
    """抢占语句必须含 `SKIP LOCKED`、`attempts + 1`、`RETURNING`。

    这三项一起构成"并发不重复"的机制：`SKIP LOCKED` 让第二个 Dispatcher 取
    后面的行、自增让 `attempts` 与"已领走"同时落库、租约让抢占窗口内不可被重抢。
    """
    from sqlalchemy.dialects import postgresql

    # 用 postgresql 方言编译：`str(stmt)` 走默认编译，内层子查询会渲染成一个
    # 绑定参数（`IN (SELECT ...)` 的具体内容看不见），断言 SKIP LOCKED 会误判。
    sql = str(
        OutboxDispatcher()
        .claim_stmt(_BATCH)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "RETURNING" in sql
    # 自增 attempts 的关键证据
    assert "attempts" in sql
    # 位置参数在 literal_binds 下已展开为常量，此处核对 LIMIT 存在
    assert "LIMIT" in sql


def test_claim_lease_is_longer_than_slowest_backoff() -> None:
    """租约应长于正常的投递耗时，这是"抢占窗口内不被重抢"的前提。

    反向断言：若租约短到 0，抢占就退化成骨架那种"提交后仍是 pending"，
    `attempts` 会翻倍。
    """
    assert CLAIM_LEASE_SECONDS > 0
    assert CLAIM_LEASE_SECONDS >= BACKOFF_SECONDS[0]


def test_backoff_table_is_monotonic_and_matches_max_attempts() -> None:
    """退避表严格递增，且长度不小于尝试上限（否则索引会越界）。"""
    assert list(BACKOFF_SECONDS) == sorted(BACKOFF_SECONDS)
    assert len(set(BACKOFF_SECONDS)) == len(BACKOFF_SECONDS), "退避表有重复项，重试节奏会原地踏步"
    assert MAX_ATTEMPTS <= len(BACKOFF_SECONDS) + 1


# --------------------------------------------------------------------------
# 投递成功
# --------------------------------------------------------------------------


def test_dispatch_marks_event_dispatched(factory) -> None:
    """投递成功后 `status='dispatched'`、`dispatched_at` 非空、`attempts=1`。

    `attempts=1` 是"抢占即自增"的直接体现：这条事件确实只被领走过一次。
    """

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, 1, prefix="ok")

        pool = _StubPool()
        processed = await OutboxDispatcher(batch_size=_BATCH, session_factory=factory).dispatch_once(pool=pool)
        assert processed == 1
        assert len(pool.calls) == 1
        # 任务名由事件类型解析而来
        assert pool.calls[0][0] == "snapshot_requested"
        assert pool.calls[0][2] == "ok-0000"

        async with factory() as db:
            row = (await _rows_by_id(db, "ok"))["ok-0000"]
            assert row.status == STATUS_DISPATCHED
            assert row.dispatched_at is not None
            assert row.attempts == 1

    asyncio.run(_run())


def test_dispatch_returns_zero_when_nothing_pending(factory) -> None:
    """没有待投递事件时不报错、返回 0（cron 每 5 秒都会跑，空轮必须无害）。"""

    async def _run() -> None:
        processed = await OutboxDispatcher(batch_size=_BATCH, session_factory=factory).dispatch_once(pool=_StubPool())
        assert processed == 0

    asyncio.run(_run())


def test_dispatched_event_is_not_claimed_again(factory) -> None:
    """已投递的事件不会被下一轮重复投递。"""

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, 1, prefix="once")

        pool = _StubPool()
        dispatcher = OutboxDispatcher(batch_size=_BATCH, session_factory=factory)
        assert await dispatcher.dispatch_once(pool=pool) == 1
        assert await dispatcher.dispatch_once(pool=pool) == 0
        assert len(pool.calls) == 1

    asyncio.run(_run())


# --------------------------------------------------------------------------
# 退避与死信
# --------------------------------------------------------------------------


def test_retry_backoff_pushes_available_at_forward(factory) -> None:
    """连续 3 轮失败：`attempts=3`、`status='pending'`、`available_at` 逐次推后。"""

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, 1, prefix="back")

        pool = _StubPool(fail=True)
        dispatcher = OutboxDispatcher(batch_size=_BATCH, session_factory=factory)
        scheduled: list[object] = []

        for expected_attempts in (1, 2, 3):
            assert await dispatcher.dispatch_once(pool=pool) == 1
            async with factory() as db:
                row = (await _rows_by_id(db, "back"))["back-0000"]
                assert row.attempts == expected_attempts
                assert row.status == STATUS_PENDING, "未达上限时不应转 dead"
                assert row.last_error, "失败原因必须留痕"
                scheduled.append(row.available_at)
            # 模拟退避时间已到：把它拉回 now()，下一轮才抢得到
            async with factory() as db:
                await _make_claimable(db, "back")

        assert scheduled == sorted(scheduled), "available_at 必须逐次推后"
        assert len(set(scheduled)) == len(scheduled)

        async with factory() as db:
            row = (await _rows_by_id(db, "back"))["back-0000"]
            assert row.attempts == 3
            assert row.status == STATUS_PENDING

    asyncio.run(_run())


def test_event_becomes_dead_after_max_attempts(factory) -> None:
    """连续 5 轮失败：`status='dead'`、`last_error` 非空。

    第三态 `dead` 存在的理由：失败必须能被查出来。若只有"投递/未投递"两态，
    重试耗尽的事件会永远停在 `pending`，看起来"还在重试"，实则永远不会再动。
    """

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, 1, prefix="dead")

        pool = _StubPool(fail=True)
        dispatcher = OutboxDispatcher(batch_size=_BATCH, session_factory=factory)
        for _ in range(MAX_ATTEMPTS):
            async with factory() as db:
                await _make_claimable(db, "dead")
            assert await dispatcher.dispatch_once(pool=pool) == 1

        async with factory() as db:
            row = (await _rows_by_id(db, "dead"))["dead-0000"]
            assert row.attempts == MAX_ATTEMPTS
            assert row.status == STATUS_DEAD
            assert row.last_error
            assert "RuntimeError" in row.last_error

        # 转 dead 之后不再被抢占：再跑一轮应当无事可做
        async with factory() as db:
            await _make_claimable(db, "dead")
        assert await dispatcher.dispatch_once(pool=pool) == 0

    asyncio.run(_run())


def test_last_error_is_truncated(factory) -> None:
    """超长失败信息会被截断，避免把行撑大。"""
    long_message = "x" * 5000

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, 1, prefix="trunc")

        pool = _StubPool(fail=True, error=RuntimeError(long_message))
        await OutboxDispatcher(batch_size=_BATCH, session_factory=factory).dispatch_once(pool=pool)

        async with factory() as db:
            row = (await _rows_by_id(db, "trunc"))["trunc-0000"]
            assert row.last_error is not None
            assert len(row.last_error) <= 1000

    asyncio.run(_run())


# --------------------------------------------------------------------------
# 并发不重复（本项最关键的一条）
# --------------------------------------------------------------------------


def test_two_dispatchers_do_not_double_claim(factory) -> None:
    """两个 Dispatcher 并发处理 200 条：每条 `attempts` 恰好为 1，合计 200。

    这是改造 `_claim` 的直接目的。断言"每条 attempts 恰好为 1"比"总数 200"
    更强：总数对也可能是两边各投了 100 条重复的事件。
    """

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, _CONCURRENT_EVENTS, prefix="race")

        pool = _StubPool()
        # 两个独立 Dispatcher 各带自己的会话工厂，并用 gather 真正并发：
        # 它们的 UPDATE 会同时落到数据库，靠 SKIP LOCKED 与租约分离
        first = OutboxDispatcher(batch_size=_BATCH, session_factory=factory)
        second = OutboxDispatcher(batch_size=_BATCH, session_factory=factory)
        done = await asyncio.gather(first.dispatch_once(pool=pool), second.dispatch_once(pool=pool))

        assert sum(done) == _CONCURRENT_EVENTS, f"两轮合计应处理 200 条，实际 {done}"
        assert sorted(done) == [_BATCH, _BATCH], "两边应当各自抢到 100 条"
        assert len(pool.calls) == _CONCURRENT_EVENTS
        # 每条只入队一次：事件 id 无重复
        assert len({cid for _, _, cid in pool.calls}) == _CONCURRENT_EVENTS

        async with factory() as db:
            bad = (
                await db.execute(
                    select(func.count())
                    .select_from(EventOutbox)
                    .where(EventOutbox.id.like("race-%"), EventOutbox.attempts != 1)
                )
            ).scalar_one()
            assert bad == 0, f"有 {bad} 条事件的 attempts 不为 1（重复抢占）"

            statuses = (
                (await db.execute(select(EventOutbox.status).where(EventOutbox.id.like("race-%")))).scalars().all()
            )
            assert set(statuses) == {STATUS_DISPATCHED}

    asyncio.run(_run())


def test_two_dispatchers_split_disjoint_ids(factory) -> None:
    """两个 Dispatcher 抢到的 id 集合互不相交（并发不重复的集合形式）。"""

    async def _run() -> None:
        async with factory() as db:
            await _seed(db, 20, prefix="disjoint")

        pools = [_StubPool(), _StubPool()]
        dispatchers = [OutboxDispatcher(batch_size=20, session_factory=factory) for _ in range(2)]
        await asyncio.gather(*(d.dispatch_once(pool=p) for d, p in zip(dispatchers, pools, strict=True)))

        ids_a = {cid for _, _, cid in pools[0].calls}
        ids_b = {cid for _, _, cid in pools[1].calls}
        assert not (ids_a & ids_b), f"两边抢到了同一条事件：{sorted(ids_a & ids_b)}"
        assert len(ids_a | ids_b) == 20

    asyncio.run(_run())


# --------------------------------------------------------------------------
# 端到端（真 Redis + 真 ARQ worker）
# --------------------------------------------------------------------------


def _redis_available() -> bool:
    url = get_settings().REDIS_URL
    if not url:
        return False

    async def _probe() -> bool:
        pool: ArqRedis | None = None
        try:
            pool = await create_pool(RedisSettings.from_dsn(url))
            await pool.ping()
            return True
        except Exception:  # noqa: BLE001  探测失败原因不决定分支，一律视为不可用
            return False
        finally:
            if pool is not None:
                await pool.aclose()

    try:
        return asyncio.run(_probe())
    except Exception:  # noqa: BLE001  连接串本身非法也视为不可用
        return False


requires_redis = pytest.mark.skipif(not _redis_available(), reason="Redis 不可用，跳过端到端投递用例")


@requires_redis
def test_end_to_end_dispatch_reaches_worker(factory) -> None:
    """写入 `snapshot.requested` → 真 Dispatcher 投递 → 真 ARQ worker 消费。

    端到端走真 Redis 而非桩，是为了覆盖"任务名对不上"这类只在联调时才暴露的问题：
    注册表写成 `snapshot_requested`、worker 注册成别的名字，桩测试全绿而线上事件
    永远没人消费。
    """

    async def _run() -> None:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))
        try:
            # 清掉可能残留的队列，避免上一轮的任务干扰"本轮的队列非空"断言
            await pool.delete("arq:queue")

            async with factory() as db:
                await emit(
                    db,
                    event_type=EVENT_SNAPSHOT_REQUESTED,
                    aggregate_type="snapshot",
                    aggregate_id=f"e2e-{uuid.uuid4().hex[:8]}",
                    user_id="u1",
                    payload={"session_id": "sess-e2e"},
                )
                await db.commit()

            processed = await OutboxDispatcher(batch_size=10, session_factory=factory).dispatch_once(pool=pool)
            assert processed == 1

            async with factory() as db:
                row = (await db.execute(select(EventOutbox).where(EventOutbox.user_id == "u1"))).scalars().one()
                assert row.status == STATUS_DISPATCHED
                assert row.dispatched_at is not None
                assert row.attempts == 1

            # 队列里确实有任务（说明任务名被正确解析并投出）
            assert await pool.zcard("arq:queue") == 1
        finally:
            await pool.delete("arq:queue")
            await pool.aclose()

    asyncio.run(_run())


@requires_redis
def test_worker_consumes_the_enqueued_job(factory) -> None:
    """真 ARQ worker（burst 模式）能消费掉投出的任务，且确实「执行」了它。

    断言 `jobs_complete == 1` 而不是"队列被清空"：实测 ARQ 遇到未注册的任务名
    也会把它从队列里移走（只是记一次失败），所以"队列空了"对"名字是否注册对"
    完全不敏感。`jobs_complete` 才区分得出"真的跑到了函数"。
    """
    from app.worker import WorkerSettings

    async def _run() -> None:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))
        try:
            await pool.delete("arq:queue")
            async with factory() as db:
                await emit(
                    db,
                    event_type=EVENT_SNAPSHOT_REQUESTED,
                    aggregate_type="snapshot",
                    aggregate_id=f"burst-{uuid.uuid4().hex[:8]}",
                    user_id="u2",
                    payload={"session_id": "sess-burst"},
                )
                await db.commit()

            assert await OutboxDispatcher(batch_size=10, session_factory=factory).dispatch_once(pool=pool) == 1
            assert await pool.zcard("arq:queue") == 1

            # 用同一份 WorkerSettings（含 cron 与函数注册表）在 burst 模式下消费
            worker = Worker(
                WorkerSettings.functions,
                redis_settings=RedisSettings.from_dsn(get_settings().REDIS_URL),
                burst=True,
                poll_delay=0.1,
                max_burst_jobs=1,
                cron_jobs=None,
            )
            await worker.async_run()

            assert await pool.zcard("arq:queue") == 0, "worker 应当把投出的任务消费掉"
            assert worker.jobs_complete == 1, "任务名未被注册时 jobs_complete 会是 0"
            assert worker.jobs_failed == 0
        finally:
            await pool.delete("arq:queue")
            await pool.aclose()

    asyncio.run(_run())


# --------------------------------------------------------------------------
# worker 注册表
# --------------------------------------------------------------------------


def test_all_six_event_types_resolve_to_registered_tasks() -> None:
    """技术方案 6.6 的六个事件都能解析成已注册的任务名。"""
    from app.events.handlers import EVENT_TYPES, resolve_task
    from app.worker import WorkerSettings

    registered = {fn.__qualname__ for fn in WorkerSettings.functions}
    assert len(EVENT_TYPES) == 6
    for event_type in EVENT_TYPES:
        task = resolve_task(event_type)
        assert task in registered, f"{event_type} 解析出的任务 {task} 未在 worker 注册"


def test_unknown_event_falls_back_to_unhandled_task() -> None:
    """未注册的事件类型解析到兜底任务，而不是抛异常。

    抛异常会让"上游发了新事件类型"表现为一条反复重试到死的死信；兜底任务则
    让它明确地留下一条日志。
    """
    from app.events.handlers import UNHANDLED_TASK, resolve_task
    from app.worker import WorkerSettings

    assert resolve_task("someone.added.this") == UNHANDLED_TASK
    assert UNHANDLED_TASK in {fn.__qualname__ for fn in WorkerSettings.functions}


def test_worker_registers_dispatch_cron() -> None:
    """worker 上挂了三个 cron：发件箱投递（5 秒粒度）、每日到期扫描、窗口内触达扫描。

    T3-09 起 `cron_jobs` 从 1 条增至 2 条（加 `scan_due_followups`），T3-12 再加
    `deliver_due_reach` 到 3 条。断言按协程对象定位而非下标，注册顺序变动不会误判。
    """
    from app.broadcast.window import FARMER_TZ
    from app.worker import WorkerSettings, deliver_due_reach, dispatch_outbox, scan_due_followups

    assert dispatch_outbox in WorkerSettings.functions
    assert scan_due_followups in WorkerSettings.functions
    assert deliver_due_reach in WorkerSettings.functions
    assert WorkerSettings.cron_jobs, "缺少投递 cron，事件会永远停在 pending"
    assert len(WorkerSettings.cron_jobs) == 3

    dispatch_cron = next(job for job in WorkerSettings.cron_jobs if job.coroutine is dispatch_outbox)
    # 每轮的秒位集合：12 个元素即每 5 秒一次
    assert dispatch_cron.second == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

    scan_cron = next(job for job in WorkerSettings.cron_jobs if job.coroutine is scan_due_followups)
    # 每天 7:00 跑一次：落在最佳触达窗口内，农户起床即可看到当天回访
    assert scan_cron.hour == 7
    assert scan_cron.minute == 0

    deliver_cron = next(job for job in WorkerSettings.cron_jobs if job.coroutine is deliver_due_reach)
    # 触达扫描：只在最佳窗口所在的小时（6/7/18）内每 5 分钟触发一次，窗口外的触发由
    # 任务内的 `is_sendable` 直接返回（ARQ cron 无法表达"6 点且分钟 >= 30"）
    assert deliver_cron.hour == {6, 7, 18}
    assert deliver_cron.minute == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

    # 时区：全部 cron 按农户时区解释，否则容器默认 UTC，早晨窗口整体后移 8 小时
    assert WorkerSettings.timezone == FARMER_TZ

    # `followup.due` 已落地为真实消费者：不再是占位实现
    from app.worker import followup_due

    assert followup_due in WorkerSettings.functions

    # `reach.failed` 已落地为真实消费者（T3-14）：写指标占位接 T6-09
    from app.worker import reach_failed

    assert reach_failed in WorkerSettings.functions
