"""T3-01 验收：提示与触达四张表的列、索引、外键、唯一约束与枚举校验。

跑在真实 PostgreSQL 上：`JSONB`、部分索引、`TIMESTAMPTZ` 与时区还原、外键违例都不是 SQLite
能力范围内的对象。四张表由迁移 `0009_prompt_snapshot` 建出（`migrated_db`），因此本用例同时
验证了"迁移与 ORM 一致"这一前提。

覆盖任务文件点名的断言：`prompt_item` 十二列逐字一致、`prompt_snapshot` / `reach_record` 的关键
列存在、两条高优先级外键指向正确且非级联删除、外键违例与唯一约束违例、跨时区的 `generated_at`
可还原、`source_kind` 非法取值在 ORM 层抛 `ValueError`。
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.models import PromptItem, PromptSnapshot, ReachRecord, SubscriptionQuota
from app.models.enums import (
    Channel,
    EnvSource,
    FailReason,
    ReachStatus,
    Severity,
    SourceKind,
    coerce_enum,
)
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("requires_postgres")


@pytest.fixture()
def sync_engine(migrated_db):
    """psycopg 同步引擎：`inspect` 与 `pg_constraint` 查询都是同步 API。"""
    from sqlalchemy import create_engine

    eng = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


@pytest.fixture()
def sync_inspector(sync_engine):
    return inspect(sync_engine)


def _snapshot(**overrides) -> PromptSnapshot:
    """一条合法的快照，逐项覆盖时只改关心的字段。"""
    base = {
        "id": "snap_1",
        "user_id": "u1",
        "farm_entry_id": "farm_1",
        "snapshot_date": date(2026, 9, 30),
        "generated_at": datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc),
        "env_source": "sensor",
    }
    base.update(overrides)
    return PromptSnapshot(**base)


def _item(**overrides) -> PromptItem:
    """一条合法的提示条目，逐项覆盖时只改关心的字段。"""
    base = {
        "id": "item_1",
        "user_id": "u1",
        "snapshot_id": "snap_1",
        "source_kind": "env_risk",
        "severity": "notice",
        "callout": "水稻",
        "fact": "近 24 小时最高温 36℃",
        "impact": "可能造成热害",
        "action": "午后灌水降温",
        "facts_provenance": {"fact": "env:sensor:2026-09-30T08:00:00"},
        "rule_id": "R-TEMP-HIGH",
        "priority_score": Decimal("2.500"),
    }
    base.update(overrides)
    return PromptItem(**base)


async def _insert(session_factory, obj) -> None:
    async with session_factory() as db:
        db.add(obj)
        await db.commit()


# ---- 枚举闭集（不连库） ----


def test_enum_values_are_closed() -> None:
    """六个枚举取值与原方案 12.5 的注释逐字一致，多一个少一个都会让契约对不上。"""
    assert {e.value for e in EnvSource} == {"sensor", "sensor_stale", "weather", "none"}
    assert {e.value for e in SourceKind} == {"env_risk", "calendar", "followup"}
    assert {e.value for e in Severity} == {"urgent", "notice", "reminder"}
    assert {e.value for e in Channel} == {"subscribe_once", "in_app"}
    assert {e.value for e in ReachStatus} == {"delivered", "failed"}
    assert {e.value for e in FailReason} == {"no_grant", "quota_exhausted", "api_error", "quiet_hours"}


def test_coerce_enum_raises_value_error_on_unknown() -> None:
    """非法取值抛 `ValueError`（不是 `LookupError`），`None` 原样透传以支持可空列。"""
    assert coerce_enum(Severity, "urgent") == "urgent"
    assert coerce_enum(Severity, Severity.NOTICE) == "notice"
    assert coerce_enum(Severity, None) is None
    with pytest.raises(ValueError):
        coerce_enum(SourceKind, "unknown_kind")


async def test_invalid_source_kind_rejected_at_orm_layer() -> None:
    """`source_kind="unknown_kind"` 在 ORM 层即抛 `ValueError`（枚举解析失败）。"""
    with pytest.raises(ValueError):
        _item(source_kind="unknown_kind")


# ---- 列与结构（连库） ----


def test_prompt_item_has_twelve_columns(sync_inspector) -> None:
    """`prompt_item` 含 12.5 定义的十二列，逐字一致。"""
    cols = {c["name"] for c in sync_inspector.get_columns("prompt_item")}
    expected = {
        "source_kind",
        "severity",
        "callout",
        "fact",
        "impact",
        "action",
        "facts_provenance",
        "rule_id",
        "priority_score",
        "suppressed",
        "executed",
        "executed_at",
    }
    assert expected <= cols


def test_prompt_snapshot_and_reach_record_columns(sync_inspector) -> None:
    """`prompt_snapshot` 与 `reach_record` 的关键列存在。"""
    snap_cols = {c["name"] for c in sync_inspector.get_columns("prompt_snapshot")}
    reach_cols = {c["name"] for c in sync_inspector.get_columns("reach_record")}
    assert {"env_source", "revision", "invalidated"} <= snap_cols
    assert {"fail_reason", "channel", "status", "attempted_at", "delivered_at"} <= reach_cols


def test_high_priority_foreign_keys(sync_engine) -> None:
    """两条高优先级外键指向正确，且 `confdeltype` 不为 `c`（非级联删除，12.7）。"""
    with sync_engine.connect() as conn:
        item_fk = conn.execute(
            text(
                "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
                "WHERE contype='f' AND conname='fk_prompt_item_snapshot'"
            )
        ).one()
        reach_fk = conn.execute(
            text(
                "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
                "WHERE contype='f' AND conname='fk_reach_record_prompt_item'"
            )
        ).one()

    assert item_fk[0] == "prompt_snapshot"
    assert reach_fk[0] == "prompt_item"
    assert item_fk[1] != "c"
    assert reach_fk[1] != "c"


def test_indexes_exist(sync_inspector) -> None:
    """任务文件的索引清单四表逐条落地（含两条部分索引）。"""
    snap_ix = {i["name"] for i in sync_inspector.get_indexes("prompt_snapshot")}
    item_ix = {i["name"] for i in sync_inspector.get_indexes("prompt_item")}
    reach_ix = {i["name"] for i in sync_inspector.get_indexes("reach_record")}

    assert {"ix_prompt_snapshot_user_id", "ix_prompt_snapshot_user_date"} <= snap_ix
    assert "ix_prompt_snapshot_user_farm_date" in snap_ix
    assert {"ix_prompt_item_user_id", "ix_prompt_item_snapshot_id", "ix_prompt_item_unsuppressed"} <= item_ix
    assert {"ix_reach_record_user_id", "ix_reach_record_prompt_item_id", "ix_reach_record_user_attempted"} <= reach_ix


# ---- 约束与违例（连库） ----


async def test_reach_record_foreign_key_violation(session_factory) -> None:
    """`prompt_item_id` 指向不存在的条目必须被外键拦下（11.8 顺序约束的数据层落地）。"""
    with pytest.raises(IntegrityError):
        await _insert(
            session_factory,
            ReachRecord(
                id="rr_1",
                user_id="u1",
                prompt_item_id="does-not-exist",
                channel=Channel.SUBSCRIBE_ONCE.value,
                status=ReachStatus.DELIVERED.value,
                attempted_at=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
                delivered_at=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
            ),
        )


async def test_snapshot_daily_unique_constraint(session_factory) -> None:
    """`(user_id, farm_entry_id, snapshot_date, revision)` 完全相同的第二条快照必须被拦下。"""
    await _insert(session_factory, _snapshot())
    with pytest.raises(IntegrityError):
        await _insert(session_factory, _snapshot(id="snap_2"))


async def test_snapshot_null_farm_partial_unique(session_factory) -> None:
    """杂草流程（`farm_entry_id IS NULL`）的同日同版本唯一性由部分唯一索引兜底。"""
    await _insert(session_factory, _snapshot(id="snap_w1", farm_entry_id=None))
    with pytest.raises(IntegrityError):
        await _insert(session_factory, _snapshot(id="snap_w2", farm_entry_id=None))


async def test_generated_at_roundtrips_with_tz(session_factory) -> None:
    """跨夏令时边界的 `generated_at` 存回后 `.tzinfo` 非空且 UTC 偏移可还原。"""
    shanghai = datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    await _insert(session_factory, _snapshot(generated_at=shanghai))

    async with session_factory() as db:
        row = (await db.execute(select(PromptSnapshot).where(PromptSnapshot.id == "snap_1"))).scalar_one()

    assert row.generated_at.tzinfo is not None
    assert row.generated_at.astimezone(timezone.utc) == shanghai.astimezone(timezone.utc)


async def test_reach_record_saved_and_fail_reason_nullable(session_factory) -> None:
    """成功触达记录可落库且 `fail_reason` 为空——失败原因列只在失败时有值。"""
    await _insert(session_factory, _snapshot())
    await _insert(session_factory, _item())
    await _insert(
        session_factory,
        ReachRecord(
            id="rr_ok",
            user_id="u1",
            prompt_item_id="item_1",
            channel=Channel.IN_APP.value,
            status=ReachStatus.DELIVERED.value,
            attempted_at=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
            delivered_at=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
        ),
    )

    async with session_factory() as db:
        row = (await db.execute(select(ReachRecord).where(ReachRecord.id == "rr_ok"))).scalar_one()

    assert row.fail_reason is None
    assert row.status == "delivered"
    assert row.prompt_item_id == "item_1"


async def test_subscription_quota_one_row_per_user(session_factory) -> None:
    """`subscription_quota` 以 `user_id` 为主键，一户一行。"""
    await _insert(session_factory, SubscriptionQuota(user_id="u1", granted_total=5, remaining=5))

    async with session_factory() as db:
        row = (await db.execute(select(SubscriptionQuota).where(SubscriptionQuota.user_id == "u1"))).scalar_one()

    assert row.remaining == 5
    assert row.reject_streak == 0
