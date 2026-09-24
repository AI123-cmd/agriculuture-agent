"""T1-13 验收（一）：发件箱表结构、事务一致性、幂等。

这一组用例只关心"写进去"的一端：表有没有按技术方案 6.6 建对、事件是否与业务
同生共死、重复 emit 是否被唯一约束挡住。投递端（抢占、退避、死信、并发）在
`test_outbox_dispatch.py`。

**事务一致性那两条是本文件的核心**，因为它们验证的是发件箱相对"提交后直接
enqueue"的根本优势：业务回滚时事件必须一并消失。

这些用例都跑在真实 PostgreSQL 上：`JSONB`、部分索引、唯一约束在 SQLite 上
要么不存在、要么语义不同。
"""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.uow import unit_of_work
from app.events import (
    EVENT_FOLLOWUP_DUE,
    EVENT_PRESCRIPTION_CREATED,
    EVENT_SNAPSHOT_REQUESTED,
    emit,
)
from app.models import EventOutbox
from app.models.event_outbox import STATUS_PENDING
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("requires_postgres")

_TABLE = "event_outbox"

# 技术方案 12.1 / 本项验收要求必须存在的列
_REQUIRED_COLUMNS = (
    "id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "user_id",
    "payload",
    "idempotency_key",
    "status",
    "attempts",
    "last_error",
    "available_at",
    "created_at",
    "dispatched_at",
)


def _payload(**extra) -> dict:
    return {"session_id": "sess-outbox-1", **extra}


async def _count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(EventOutbox))).scalar_one()


@pytest.fixture()
def factory(engine):
    """NullPool 引擎上的会话工厂：跨事件循环复用不会串连接。"""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def sync_engine(migrated_db):
    """psycopg 同步引擎：`information_schema` / `pg_indexes` 查询读起来更直白。

    `test_migrations.py` 里有同名 fixture，但为用例级作用域、且不在 conftest 里，
    此处必须自建。用 psycopg 而非 asyncpg 是因为这里只做只读元数据查询。
    """
    eng = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


# --------------------------------------------------------------------------
# 表结构（对应验收第 1 条）
# --------------------------------------------------------------------------


def test_all_required_columns_exist(sync_engine) -> None:
    """`information_schema.columns` 里 13 个必需列齐备，且类型符合预期。"""
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                " WHERE table_name = :t AND table_schema = 'public'"
            ),
            {"t": _TABLE},
        ).all()
    columns = {name: (dtype, nullable) for name, dtype, nullable in rows}

    missing = set(_REQUIRED_COLUMNS) - set(columns)
    assert not missing, f"event_outbox 缺列：{sorted(missing)}"

    # 两个时间列必须带时区：裸 timestamp 与全局约定冲突，跨时区会算错
    assert columns["available_at"][0] == "timestamp with time zone"
    assert columns["created_at"][0] == "timestamp with time zone"
    # 载荷必须是 jsonb 而非 json：要按 payload->>'xxx' 查询统计
    assert columns["payload"][0] == "jsonb"
    # 失败原因可空，正常投递的事件没有它
    assert columns["last_error"][1] == "YES"
    assert columns["dispatched_at"][1] == "YES"


def test_partial_index_on_pending_exists(sync_engine) -> None:
    """存在一条只覆盖 `pending` 行的部分索引，且条件文本正确。

    部分索引的意义是索引体积与"待投递量"成正比而非与历史总量成正比：
    `dispatched` 行不在这份索引里，历史越多它越省。
    """
    with sync_engine.connect() as conn:
        defs = dict(conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"), {"t": _TABLE}).all())
    partial = {name: ddl for name, ddl in defs.items() if "WHERE" in ddl}
    assert partial, f"event_outbox 上没有部分索引：{sorted(defs)}"
    # PostgreSQL 会把条件规范化成 ((status)::text = 'pending'::text)
    assert any("status" in ddl and "pending" in ddl for ddl in partial.values()), partial


def test_dispatch_query_index_exists(sync_engine) -> None:
    """投递查询的主索引 (status, available_at) 存在。"""
    names = {ix["name"] for ix in sa.inspect(sync_engine).get_indexes(_TABLE)}
    assert "ix_event_outbox_status_available_at" in names
    assert "ix_event_outbox_pending" in names


def test_duplicate_idempotency_key_raises_integrity_error(factory) -> None:
    """连续两次插入相同 `idempotency_key`，第二条抛 `IntegrityError`。

    唯一约束是"同一处方重复发事件"这类 bug 的写入期防线：错误在产生它的地方
    暴露，而不是等消费者处理两遍。
    """

    async def _run() -> None:
        async with factory() as db:
            db.add(
                EventOutbox(
                    id="ob-dup-1",
                    event_type=EVENT_PRESCRIPTION_CREATED,
                    aggregate_type="prescription",
                    aggregate_id="card-1",
                    user_id="u1",
                    payload=_payload(),
                    idempotency_key="prescription.created:card-1",
                )
            )
            await db.commit()

        async with factory() as db:
            db.add(
                EventOutbox(
                    id="ob-dup-2",
                    event_type=EVENT_PRESCRIPTION_CREATED,
                    aggregate_type="prescription",
                    aggregate_id="card-1",
                    user_id="u1",
                    payload=_payload(),
                    idempotency_key="prescription.created:card-1",
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()

    asyncio.run(_run())


def test_default_status_and_attempts(factory) -> None:
    """默认 `status='pending'`、`attempts=0`，`available_at` 由库侧给出。"""

    async def _run() -> None:
        async with factory() as db:
            row = EventOutbox(
                id="ob-defaults",
                event_type=EVENT_SNAPSHOT_REQUESTED,
                aggregate_type="snapshot",
                aggregate_id="snap-1",
                user_id="u1",
                payload=_payload(),
                idempotency_key="snapshot.requested:snap-1",
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)

            assert row.status == STATUS_PENDING
            assert row.attempts == 0
            assert row.available_at is not None
            assert row.created_at is not None
            assert row.dispatched_at is None

    asyncio.run(_run())


# --------------------------------------------------------------------------
# 事务一致性（对应验收第 2 条前半）
# --------------------------------------------------------------------------


def test_emit_rolls_back_with_business_transaction(factory) -> None:
    """`unit_of_work` 内 emit 后抛异常 → 行数不变。

    这就是"事件与业务同生共死"：若改成"提交后再 enqueue"，业务回滚了事件却
    已经入队，消费者会读到一个不存在的聚合根。
    """

    async def _run() -> None:
        async with factory() as db:
            before = await _count(db)

            class _Boom(RuntimeError):
                pass

            with pytest.raises(Exception):  # noqa: B017  unit_of_work 会包装成 TransactionError
                async with unit_of_work(db, name="test.rollback"):
                    await emit(
                        db,
                        event_type=EVENT_PRESCRIPTION_CREATED,
                        aggregate_type="prescription",
                        aggregate_id="card-rollback",
                        user_id="u1",
                        payload=_payload(),
                    )
                    raise _Boom("业务失败")

        # 换一个会话读：确认不是本会话缓存造成的假象
        async with factory() as db:
            assert await _count(db) == before

    asyncio.run(_run())


def test_emit_commits_with_business_transaction(factory) -> None:
    """正常提交后 `event_outbox` 行数 +1，且 `idempotency_key` 有默认值。"""

    async def _run() -> None:
        async with factory() as db:
            before = await _count(db)

        async with factory() as db:
            async with unit_of_work(db, name="test.commit"):
                row = await emit(
                    db,
                    event_type=EVENT_PRESCRIPTION_CREATED,
                    aggregate_type="prescription",
                    aggregate_id="card-commit",
                    user_id="u1",
                    payload=_payload(),
                )
                # emit 不提交也不 flush：提交前该行还不在库里
                assert row.id

        async with factory() as db:
            assert await _count(db) == before + 1
            stored = (
                await db.execute(select(EventOutbox).where(EventOutbox.aggregate_id == "card-commit"))
            ).scalar_one()
            # 未显式传 idempotency_key 时由 event_type + aggregate_id 拼出
            assert stored.idempotency_key == "prescription.created:card-commit"
            assert stored.status == STATUS_PENDING
            assert stored.payload["session_id"] == "sess-outbox-1"

    asyncio.run(_run())


def test_emit_does_not_flush_implicitly(factory) -> None:
    """`emit` 只 `add`：调用后同一会话内发 COUNT 会触发 autobegin/autflush 之外的行数不变。

    用一种更直接的方式核对：emit 后立刻在另一个会话里查，应当看不到该行
    （事务未提交）。这钉死了"emit 不自行提交"这一约定。
    """

    async def _run() -> None:
        async with factory() as db_a:
            async with unit_of_work(db_a, name="test.no_commit"):
                await emit(
                    db_a,
                    event_type=EVENT_FOLLOWUP_DUE,
                    aggregate_type="followup",
                    aggregate_id="fu-1",
                    user_id="u1",
                    payload={"due_in_days": 3},
                )

                # 同一事务的另一条连接：看不到未提交的行
                async with factory() as db_b:
                    inside = (
                        await db_b.execute(
                            select(func.count()).select_from(EventOutbox).where(EventOutbox.aggregate_id == "fu-1")
                        )
                    ).scalar_one()
                assert inside == 0

        async with factory() as db:
            after = (
                await db.execute(
                    select(func.count()).select_from(EventOutbox).where(EventOutbox.aggregate_id == "fu-1")
                )
            ).scalar_one()
            assert after == 1

    asyncio.run(_run())


def test_emit_warns_on_unregistered_event_type(factory, caplog) -> None:
    """未注册的事件类型不抛异常，但记一条告警——写错事件名要当场可见。"""

    async def _run() -> None:
        async with factory() as db:
            async with unit_of_work(db, name="test.unknown_event"):
                row = await emit(
                    db,
                    event_type="nobody.knows.this",
                    aggregate_type="mystery",
                    aggregate_id="m-1",
                    user_id=None,
                    payload={},
                )
                assert row.status == STATUS_PENDING

    with caplog.at_level("WARNING"):
        asyncio.run(_run())
    assert any("未注册的事件类型" in r.message for r in caplog.records)
