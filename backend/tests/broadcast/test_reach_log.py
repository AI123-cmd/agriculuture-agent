"""T3-14 验收：触达记录与失败原因分类。

覆盖验收标准的六个点：`classify_fail` 四类判据与成功返回 `None`、记录完整性（成功与四类失败
各落一条，`status`/`fail_reason` 组合非法时模型校验抛错）、不可静默丢弃（推送主流程抛非预期
异常仍留下 `api_error` 记录）、指标口径（`delivered_rate` 分母剔除 `quiet_hours`、
`quiet_hours_rate` 分母为全部尝试数）、记录失败不影响推送、观测隔离（触达记录不被生成或排序
逻辑引用）。

数据访问一律走仓储（`engine` / `session_factory` 夹具）。纯分类与隔离用例不起数据库：
`classify_fail` 是纯函数，隔离用例只读源码文本，二者声明 `engine` 之外不触碰库，无 PG 时
不被跳过。
"""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.broadcast.notifier as notifier
from app.broadcast.notifier import deliver_due_items, notify_item
from app.broadcast.reach_log import (
    FAIL_API_ERROR,
    FAIL_NO_GRANT,
    FAIL_QUIET_HOURS,
    FAIL_QUOTA_EXHAUSTED,
    classify_fail,
    reach_metrics,
    record_attempt,
)
from app.db.base import new_id
from app.models import PromptItem, PromptSnapshot, ReachRecord, SubscriptionQuota, User
from app.repositories.reach import reach_repo

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 06:45 落在最佳窗口（06:30~07:30）内；23:00 落在静默时段（22:00~06:00）。
IN_WINDOW = datetime(2026, 5, 1, 6, 45, tzinfo=SHANGHAI)
QUIET = datetime(2026, 5, 1, 23, 0, tzinfo=SHANGHAI)

USER = "user-t314"


# ---- 夹具与构造 ----


def _user(user_id: str = USER) -> User:
    return User(id=user_id, openid=f"openid-{user_id}")


def _snapshot(*, user_id: str = USER, snapshot_date: date | None = None) -> PromptSnapshot:
    return PromptSnapshot(
        id=new_id("snap"),
        farm_entry_id=None,
        snapshot_date=snapshot_date or date.today(),
        generated_at=datetime.now(UTC),
        env_source="sensor",
        revision=1,
        invalidated=False,
        user_id=user_id,
    )


def _item(*, user_id: str = USER, snapshot_id: str, rule_id: str = "env_risk.heat") -> PromptItem:
    return PromptItem(
        id=new_id("item"),
        snapshot_id=snapshot_id,
        source_kind="env_risk",
        severity="notice",
        callout="东边水田",
        fact="气温 35.0℃，超过阈值 30.0℃",
        impact="持续高温影响结实",
        action="灌深水降温",
        facts_provenance={},
        rule_id=rule_id,
        priority_score=Decimal("1.000"),
        suppressed=False,
        executed=False,
        user_id=user_id,
    )


def _quota(*, user_id: str = USER, granted_total: int = 1, remaining: int = 1) -> SubscriptionQuota:
    return SubscriptionQuota(
        user_id=user_id,
        granted_total=granted_total,
        consumed_total=granted_total - remaining,
        remaining=remaining,
        reject_streak=0,
    )


async def _persist(engine, *objects: object) -> None:
    """逐对象 `add` + `flush` 落库：表间只有外键、无 ORM relationship，需保证落库顺序。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


@pytest.fixture()
async def seeded(engine):
    """一条用户 + 一份当日快照 + 一条提示条目，供记录类用例复用。"""
    user = _user()
    snapshot = _snapshot()
    item = _item(snapshot_id=snapshot.id)
    await _persist(engine, user, snapshot, item)
    return {"user_id": user.id, "snapshot_id": snapshot.id, "item_id": item.id}


async def _records(session_factory, item_id: str) -> list[ReachRecord]:
    async with session_factory() as db:
        stmt = select(ReachRecord).where(ReachRecord.prompt_item_id == item_id)
        return list((await db.execute(stmt)).scalars().all())


# ---- 一、classify_fail 四类判据（验收标准第一段） ----


def test_classify_quiet_hours_wins_first() -> None:
    """时段不可发送优先级最高：即使额度也判无授权，仍返回 `quiet_hours`。"""
    assert classify_fail(sendable=False, consumed=False, granted_total=0, exc=None) == FAIL_QUIET_HOURS


def test_classify_no_grant() -> None:
    """可发送但未消费到额度且从未授权（`granted_total == 0`）→ `no_grant`。"""
    assert classify_fail(sendable=True, consumed=False, granted_total=0, exc=None) == FAIL_NO_GRANT


def test_classify_quota_exhausted() -> None:
    """可发送但未消费到额度且有过授权（`granted_total > 0`）→ `quota_exhausted`。"""
    assert classify_fail(sendable=True, consumed=False, granted_total=2, exc=None) == FAIL_QUOTA_EXHAUSTED


def test_classify_api_error() -> None:
    """消费到额度但抛异常 → `api_error`。"""
    assert classify_fail(sendable=True, consumed=True, granted_total=1, exc=RuntimeError("boom")) == FAIL_API_ERROR


def test_classify_success_returns_none() -> None:
    """全部通过 → 成功，返回 `None`。"""
    assert classify_fail(sendable=True, consumed=True, granted_total=1, exc=None) is None


# ---- 二、记录完整性（验收标准第二段） ----


async def test_record_attempt_success_has_no_reason_and_delivered_at(engine, session_factory, seeded) -> None:
    """成功记录：`status="delivered"`、`fail_reason is None`、`delivered_at` 非空。"""
    async with session_factory() as db:
        record = await record_attempt(
            db,
            user_id=seeded["user_id"],
            prompt_item_id=seeded["item_id"],
            channel="subscribe_once",
            fail_reason=None,
        )
        await db.commit()

    assert record.status == "delivered"
    assert record.fail_reason is None
    assert record.delivered_at is not None


@pytest.mark.parametrize("reason", [FAIL_NO_GRANT, FAIL_QUOTA_EXHAUSTED, FAIL_API_ERROR, FAIL_QUIET_HOURS])
async def test_record_attempt_failure_sets_reason_and_no_delivered_at(
    engine, session_factory, seeded, reason: str
) -> None:
    """四类失败记录：`status="failed"`、`fail_reason` 为该类、`delivered_at` 为空。"""
    async with session_factory() as db:
        record = await record_attempt(
            db,
            user_id=seeded["user_id"],
            prompt_item_id=seeded["item_id"],
            channel="subscribe_once",
            fail_reason=reason,
        )
        await db.commit()

    assert record.status == "failed"
    assert record.fail_reason == reason
    assert record.delivered_at is None


def test_failed_without_reason_is_rejected_by_model() -> None:
    """`status="failed"` 但 `fail_reason=None` 的组合被模型校验器拦下（不可静默丢弃的模型层保障）。"""
    with pytest.raises(ValueError, match="fail_reason 不能为空"):
        ReachRecord(
            id=new_id("rr"),
            user_id=USER,
            prompt_item_id="item-x",
            channel="subscribe_once",
            status="failed",
            fail_reason=None,
            attempted_at=datetime.now(UTC),
        )


# ---- 三、不可静默丢弃：推送主流程抛非预期异常仍留下记录（验收标准第三段） ----


async def test_unexpected_exception_still_leaves_api_error_record(engine, session_factory, seeded, monkeypatch) -> None:
    """让微信发送抛非预期异常：条目仍有 `reach_record`（`api_error`）而非无记录。"""

    async def _boom(*, openid: str, data: dict, page: str | None = None, template_id: str | None = None) -> None:
        raise RuntimeError("谁也想不到的异常")

    monkeypatch.setattr(notifier, "send_subscribe_message", _boom)

    quota = _quota(remaining=1)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        db.add(quota)
        await db.commit()

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert record.fail_reason == FAIL_API_ERROR
    assert await _records(session_factory, seeded["item_id"]), "推送异常也必须留下一条触达记录"


# ---- 四、指标口径（验收标准第四段） ----


async def test_reach_metrics_denominators(engine, session_factory, seeded) -> None:
    """6 delivered + 2 no_grant + 1 quota_exhausted + 1 api_error + 4 quiet_hours。

    `delivered_rate` 的分母是"已尝试且未被时段延迟"的条目数（14-4=10）→ 6/10=0.6；
    `quiet_hours_rate` 的分母是全部尝试数（14）。`total = delivered + sum(failed)` 由
    `reach_metrics` 固定，不留给调用方（口径与 T6-09 对齐）。
    """
    item_id = seeded["item_id"]
    user_id = seeded["user_id"]
    reasons = [None] * 6 + [FAIL_NO_GRANT] * 2 + [FAIL_QUOTA_EXHAUSTED, FAIL_API_ERROR] + [FAIL_QUIET_HOURS] * 4

    async with session_factory() as db:
        for reason in reasons:
            await record_attempt(
                db,
                user_id=user_id,
                prompt_item_id=item_id,
                channel="subscribe_once",
                fail_reason=reason,
            )
        await db.commit()

    # `since` 取昨天而非今天：记录的时刻是农户时区的"现在"，取昨天零点可稳定纳入（含凌晨）。
    async with session_factory() as db:
        metrics = await reach_metrics(db, date.today() - timedelta(days=1))

    assert metrics["delivered_rate"] == pytest.approx(6 / 10)
    assert metrics["no_grant_rate"] == pytest.approx(2 / 14)
    assert metrics["quota_exhausted_rate"] == pytest.approx(1 / 14)
    assert metrics["api_error_rate"] == pytest.approx(1 / 14)
    assert metrics["quiet_hours_rate"] == pytest.approx(4 / 14)


# ---- 五、记录失败不影响推送（验收标准第五段） ----


async def test_record_failure_does_not_break_delivery(engine, session_factory, seeded, monkeypatch) -> None:
    """让 `reach_repo.save` 抛异常：`deliver_due_items` 仍返回正常统计且不抛异常。"""

    async def _boom(db, record):  # noqa: ANN001, ANN201
        raise RuntimeError("数据库连接抖动")

    monkeypatch.setattr(reach_repo, "save", _boom)
    # 强制走静默分支：该分支直接落一条记录，是 `record_attempt` 最纯粹的调用点。
    monkeypatch.setattr(notifier, "is_sendable", lambda now: False)

    async with session_factory() as db:
        stats = await deliver_due_items(db)

    assert stats == {"delivered": 0, "skipped": 0, "failed": 1}, "记录写入失败应计入 failed 且不中断整批"


# ---- 六、观测隔离（验收标准第六段） ----


def test_reach_record_not_referenced_by_generation_or_priority() -> None:
    """触达记录是纯观测数据：生成（inputs/）与排序（priority.py）不得引用 `reach_record`。"""
    broadcast = Path(__file__).resolve().parents[2] / "app" / "broadcast"
    targets = list((broadcast / "inputs").glob("*.py")) + [broadcast / "priority.py"]
    offenders = [p.name for p in targets if "reach_record" in p.read_text(encoding="utf-8")]
    assert not offenders, f"触达记录被生成或排序逻辑引用：{offenders}"


# ---- 七、凭证缺失不落库、只记日志（record_attempt 的 note 语义） ----


async def test_record_attempt_logs_note_without_persisting(engine, session_factory, seeded, caplog) -> None:
    """`note`（微信错误码原文）只写日志不落库——`reach_record` 无 `note` 列，业务判据不依赖它。"""
    with caplog.at_level(logging.INFO, logger="app.broadcast.reach_log"):
        async with session_factory() as db:
            await record_attempt(
                db,
                user_id=seeded["user_id"],
                prompt_item_id=seeded["item_id"],
                channel="subscribe_once",
                fail_reason=FAIL_API_ERROR,
                note="45011",
            )
            await db.commit()

    assert "45011" in caplog.text
    assert not hasattr(ReachRecord, "note")
