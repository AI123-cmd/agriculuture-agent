"""T3-12 验收：一次性订阅消息发送链路。

覆盖验收标准的六个点：顺序约束（`notify_item` 无文案参数、内容取自 `prompt_item`、外键强制）、
四道门槛的顺序（时段先于额度、无额度不调微信接口、静默只延迟）、失败退还（`api_error` 退额度并
投 `reach.failed`）、失败隔离（单条失败不中断整批）、用户拒绝（`43101` 清零额度记 `no_grant`）。

微信接口一律用桩替换（`monkeypatch` 掉 `notifier.send_subscribe_message`），不起外网：本链路的
正确性完全不依赖微信可达。数据访问走仓储（`engine` / `session_factory` 夹具）。
"""

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.broadcast.notifier as notifier
from app.broadcast.notifier import deliver_due_items, notify_item
from app.db.base import new_id
from app.events.handlers import EVENT_REACH_FAILED, EVENT_SUBSCRIPTION_CONSUMED
from app.integrations.wechat import WechatApiError, WechatUserRefusedError
from app.models import EventOutbox, PromptItem, PromptSnapshot, ReachRecord, SubscriptionQuota, User

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 06:45 落在最佳窗口（06:30~07:30）内；23:00 落在静默时段（22:00~06:00）。
IN_WINDOW = datetime(2026, 5, 1, 6, 45, tzinfo=SHANGHAI)
QUIET = datetime(2026, 5, 1, 23, 0, tzinfo=SHANGHAI)


# ---- 桩：记录调用并按 openid 决定成败 ----


class _SendRecorder:
    """`send_subscribe_message` 的替身：记录每次调用，可按 openid 抛指定异常。"""

    def __init__(self, *, fail_openid: str | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_openid = fail_openid
        self._exc = exc

    async def __call__(
        self, *, openid: str, data: dict, page: str | None = None, template_id: str | None = None
    ) -> None:
        self.calls.append({"openid": openid, "data": data, "page": page, "template_id": template_id})
        if self._fail_openid is not None and openid == self._fail_openid:
            assert self._exc is not None
            raise self._exc


# ---- 构造与落库 ----


def _user(user_id: str) -> User:
    return User(id=user_id, openid=f"openid-{user_id}")


def _snapshot(user_id: str) -> PromptSnapshot:
    return PromptSnapshot(
        id=new_id("snap"),
        farm_entry_id=None,
        snapshot_date=date.today(),
        generated_at=datetime.now(UTC),
        env_source="sensor",
        revision=1,
        invalidated=False,
        user_id=user_id,
    )


def _item(user_id: str, snapshot_id: str, *, priority: str = "1.000") -> PromptItem:
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
        rule_id="env_risk.heat",
        priority_score=Decimal(priority),
        suppressed=False,
        executed=False,
        user_id=user_id,
    )


def _quota(user_id: str, *, granted_total: int = 1, remaining: int = 1) -> SubscriptionQuota:
    return SubscriptionQuota(
        user_id=user_id,
        granted_total=granted_total,
        consumed_total=granted_total - remaining,
        remaining=remaining,
        reject_streak=0,
    )


async def _persist(engine, *objects: object) -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


async def _seed(engine, user_id: str, *, granted_total: int = 1, remaining: int = 1) -> dict:
    """落一户 + 一份当日快照 + 一条条目 + 一份额度，返回 id 便于调用。"""
    user = _user(user_id)
    snapshot = _snapshot(user_id)
    item = _item(user_id, snapshot.id)
    quota = _quota(user_id, granted_total=granted_total, remaining=remaining)
    await _persist(engine, user, snapshot, item, quota)
    return {"user_id": user_id, "item_id": item.id, "snapshot_id": snapshot.id}


async def _quota_remaining(session_factory, user_id: str) -> int:
    async with session_factory() as db:
        quota = (
            (await db.execute(select(SubscriptionQuota).where(SubscriptionQuota.user_id == user_id))).scalars().one()
        )
        return quota.remaining


async def _events(session_factory, event_type: str) -> list[EventOutbox]:
    async with session_factory() as db:
        stmt = select(EventOutbox).where(EventOutbox.event_type == event_type)
        return list((await db.execute(stmt)).scalars().all())


# ---- 一、顺序约束 ----


def test_notify_item_signature_has_no_text_param() -> None:
    """`notify_item` 签名刻意不含文案参数（顺序约束的第一层）。"""
    params = inspect.signature(notify_item).parameters
    assert "text" not in params
    assert "content" not in params


async def test_notify_item_reads_content_from_prompt_item(engine, session_factory, monkeypatch) -> None:
    """推送内容取自已落库的 `prompt_item`：发送的模板字段等于该条目的 `callout/fact/action`。"""
    seeded = await _seed(engine, "user-t312-a")
    recorder = _SendRecorder()
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert record.prompt_item_id == seeded["item_id"]
    assert record.status == "delivered"
    assert record.fail_reason is None
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["data"]["thing1"]["value"] == "东边水田"
    assert recorder.calls[0]["data"]["thing3"]["value"] == "灌深水降温"


async def test_reach_record_requires_existing_prompt_item(engine, session_factory) -> None:
    """`reach_record.prompt_item_id` 的外键强制：凭空构造的条目 id 在写入时即失败。"""
    async with session_factory() as db:
        db.add(
            ReachRecord(
                id=new_id("rr"),
                user_id="user-t312-fk",
                prompt_item_id="item-does-not-exist",
                channel="subscribe_once",
                status="delivered",
                fail_reason=None,
                attempted_at=datetime.now(UTC),
                delivered_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


# ---- 二、门槛顺序（时段先于额度，无额度不调接口） ----


async def test_quiet_hours_skips_before_quota_and_interface(engine, session_factory, monkeypatch) -> None:
    """静默时段：不调微信接口、记 `quiet_hours`、额度未变、不投 `reach.failed`。"""
    seeded = await _seed(engine, "user-t312-quiet")
    recorder = _SendRecorder()
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=QUIET)

    assert record.fail_reason == "quiet_hours"
    assert recorder.calls == []
    assert await _quota_remaining(session_factory, seeded["user_id"]) == 1
    assert await _events(session_factory, EVENT_REACH_FAILED) == []


async def test_quota_exhausted_skips_interface(engine, session_factory, monkeypatch) -> None:
    """额度已用完（`granted_total>0, remaining=0`）：不调接口、记 `quota_exhausted`、投 `reach.failed`。"""
    seeded = await _seed(engine, "user-t312-exhausted", granted_total=2, remaining=0)
    recorder = _SendRecorder()
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert record.fail_reason == "quota_exhausted"
    assert recorder.calls == []
    assert len(await _events(session_factory, EVENT_REACH_FAILED)) == 1


async def test_no_grant_distinguished_from_exhausted(engine, session_factory, monkeypatch) -> None:
    """从未授权（`granted_total=0`）：记 `no_grant` 而非 `quota_exhausted`（两者界面说明不同）。"""
    seeded = await _seed(engine, "user-t312-nogrant", granted_total=0, remaining=0)
    recorder = _SendRecorder()
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert record.fail_reason == "no_grant"
    assert recorder.calls == []


# ---- 三、失败退还与事件 ----


async def test_api_error_refunds_and_emits_reach_failed(engine, session_factory, monkeypatch) -> None:
    """微信接口抛 `WechatApiError`：退还额度、记 `api_error`、投 `reach.failed`。"""
    seeded = await _seed(engine, "user-t312-apierr", granted_total=1, remaining=1)
    recorder = _SendRecorder(fail_openid="openid-user-t312-apierr", exc=WechatApiError("45011 频率限制"))
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert record.fail_reason == "api_error"
    # 先扣后失败 → 退还，remaining 回到调用前的 1
    assert await _quota_remaining(session_factory, seeded["user_id"]) == 1
    assert len(await _events(session_factory, EVENT_REACH_FAILED)) == 1


async def test_success_emits_subscription_consumed(engine, session_factory, monkeypatch) -> None:
    """成功：投 `subscription.consumed`，不投 `reach.failed`，额度被消费。"""
    seeded = await _seed(engine, "user-t312-ok", granted_total=1, remaining=1)
    monkeypatch.setattr(notifier, "send_subscribe_message", _SendRecorder())

    async with session_factory() as db:
        await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert len(await _events(session_factory, EVENT_SUBSCRIPTION_CONSUMED)) == 1
    assert await _events(session_factory, EVENT_REACH_FAILED) == []
    assert await _quota_remaining(session_factory, seeded["user_id"]) == 0


# ---- 四、用户拒绝（43101） ----


async def test_user_refused_zeroes_quota_and_records_no_grant(engine, session_factory, monkeypatch) -> None:
    """`43101`：该户额度清零、记 `no_grant`（授权已失效，退还只会反复失败）。"""
    seeded = await _seed(engine, "user-t312-refused", granted_total=3, remaining=3)
    recorder = _SendRecorder(
        fail_openid="openid-user-t312-refused",
        exc=WechatUserRefusedError("43101 农户拒绝接收", errcode=43101),
    )
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)

    async with session_factory() as db:
        record = await notify_item(db, seeded["item_id"], now=IN_WINDOW)

    assert record.fail_reason == "no_grant"
    assert await _quota_remaining(session_factory, seeded["user_id"]) == 0


# ---- 五、失败隔离 ----


async def test_deliver_due_items_isolates_single_failure(engine, session_factory, monkeypatch) -> None:
    """三条中第二条失败：另两条仍送达，`failed` 计数为 1，函数本身不抛异常。"""
    a = await _seed(engine, "user-t312-iso-a")
    b = await _seed(engine, "user-t312-iso-b")
    c = await _seed(engine, "user-t312-iso-c")
    # 让 b 的推送抛异常；a / c 正常
    recorder = _SendRecorder(fail_openid="openid-user-t312-iso-b", exc=WechatApiError("上游抖动"))
    monkeypatch.setattr(notifier, "send_subscribe_message", recorder)
    # 强制走可发送分支：`deliver_due_items` 内部用真实 now，窗口判断与环境相关
    monkeypatch.setattr(notifier, "is_sendable", lambda now: True)

    async with session_factory() as db:
        stats = await deliver_due_items(db)

    assert stats["delivered"] == 2
    assert stats["failed"] == 1
    assert stats["skipped"] == 0
    # 三条各留一条记录，失败那条也落了库（不可静默丢弃）
    async with session_factory() as db:
        records = list((await db.execute(select(ReachRecord))).scalars().all())
    assert len(records) == 3
    assert {a["user_id"], b["user_id"], c["user_id"]} == {r.user_id for r in records}
