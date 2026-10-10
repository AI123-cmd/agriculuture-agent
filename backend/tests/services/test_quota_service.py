"""T3-11 验收：订阅授权接口与额度模型（技术方案 11.7）。

把任务文件点名的断言逐条钉死：

1. **授权一次、消费一次**：`grant(granted=True)` 后 `granted_total` 与 `remaining` 各加一、
   `reject_streak` 归零；`try_consume` 在有额度时返回 `True` 且 `remaining` 减一，额度为 0 时
   返回 `False` 且各计数不变。
2. **并发扣减不双花**：`remaining=1` 时并发 10 次 `try_consume`，恰好 1 次成功、9 次失败，
   最终 `consumed_total == 1`（这是"带条件的单条 UPDATE"的意义所在）。
3. **降级索要**：`remaining=0` 且 `reject_streak=3` 时降级；有额度或未满 3 天都不降级。
4. **退还**：消费成功后 `refund` 让 `remaining` 与 `consumed_total` 回到调用前；无可退记录
   时 `refund` 是无操作，绝不把 `remaining` 变成负数。
5. **接口归属**：另一用户令牌读不到该用户额度；`granted=false` 后额度不变、`reject_streak` 加一。
6. **契约不含 user_id**：`QuotaView.model_fields` 与 `GrantRequest.model_fields` 都不含
   `user_id`（额度只能来自令牌）。

需要真实 PostgreSQL：全部断言都落在 `subscription_quota` 的真实读写上，并发扣减与
`INSERT ... ON CONFLICT ... RETURNING` 都是 PG 语义。
"""

from datetime import date

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import SubscriptionQuota
from app.schemas.subscription import GrantRequest, QuotaView
from app.services import quota_service
from app.services.quota_service import WEAKEN_AFTER_DAYS, get_quota, grant, refund, try_consume

pytestmark = pytest.mark.usefixtures("requires_postgres")

USER = "user-t311"
OTHER = "user-t311-other"
TODAY = date(2026, 5, 20)


async def _seed_quota(
    engine,
    *,
    user_id: str = USER,
    granted_total: int = 0,
    consumed_total: int = 0,
    remaining: int = 0,
    reject_streak: int = 0,
) -> None:
    """直接建一行额度，供"从某状态出发"的断言使用。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            SubscriptionQuota(
                user_id=user_id,
                granted_total=granted_total,
                consumed_total=consumed_total,
                remaining=remaining,
                reject_streak=reject_streak,
            )
        )
        await db.commit()


async def _read(engine, user_id: str = USER) -> SubscriptionQuota | None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        return (
            (await db.execute(select(SubscriptionQuota).where(SubscriptionQuota.user_id == user_id))).scalars().first()
        )


# ---- 一、授权一次、消费一次 ----


async def test_grant_true_increments_and_clears_reject_streak(engine, session_factory) -> None:
    """授权成功：granted_total 与 remaining 各加一、reject_streak 归零。"""
    await _seed_quota(engine, granted_total=2, consumed_total=2, remaining=0, reject_streak=2)

    async with session_factory() as db:
        view = await grant(db, USER, GrantRequest(granted=True))

    assert view.remaining == 1
    assert view.grantedTotal == 3
    assert view.consumedTotal == 2

    row = await _read(engine)
    assert row is not None
    assert row.remaining == 1 and row.granted_total == 3 and row.reject_streak == 0


async def test_try_consume_success_decrements(engine, session_factory) -> None:
    """有额度：返回 True，remaining 减一、consumed_total 加一。"""
    await _seed_quota(engine, granted_total=1, consumed_total=0, remaining=1)

    async with session_factory() as db:
        ok = await try_consume(db, USER)

    assert ok is True
    row = await _read(engine)
    assert row is not None
    assert row.remaining == 0 and row.consumed_total == 1


async def test_try_consume_fail_keeps_counts(engine, session_factory) -> None:
    """额度为 0：返回 False，各计数不变。"""
    await _seed_quota(engine, granted_total=1, consumed_total=1, remaining=0)

    async with session_factory() as db:
        ok = await try_consume(db, USER)

    assert ok is False
    row = await _read(engine)
    assert row is not None
    assert row.remaining == 0 and row.consumed_total == 1 and row.granted_total == 1


async def test_try_consume_without_row_returns_false(engine, session_factory) -> None:
    """从未有过额度行：返回 False（视同无额度），不建行、不报错。"""
    async with session_factory() as db:
        ok = await try_consume(db, USER)

    assert ok is False
    assert await _read(engine) is None


# ---- 二、并发扣减不双花 ----


async def test_concurrent_consume_only_one_succeeds(engine) -> None:
    """remaining=1 时并发 10 次扣减：恰好 1 次成功，consumed_total == 1。

    这是"带条件的单条 UPDATE"的核心意义：若拆成"先读再写"，10 个并发请求都会读到
    remaining=1 并判定可发，最终发出 10 条而额度只扣 1 次（双花）。
    """
    await _seed_quota(engine, granted_total=1, consumed_total=0, remaining=1)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _consume() -> bool:
        async with factory() as db:
            return await try_consume(db, USER)

    results = await asyncio.gather(*[_consume() for _ in range(10)])

    assert sum(1 for hit in results if hit) == 1
    assert sum(1 for hit in results if not hit) == 9
    row = await _read(engine)
    assert row is not None
    assert row.consumed_total == 1 and row.remaining == 0


# ---- 三、降级索要 ----


def _quota(remaining: int, reject_streak: int) -> SubscriptionQuota:
    return SubscriptionQuota(
        user_id=USER,
        granted_total=0,
        consumed_total=0,
        remaining=remaining,
        reject_streak=reject_streak,
    )


def test_should_weaken_true_when_exhausted_and_streak_reached() -> None:
    """无额度且连续拒绝达阈值：降级为弱提示。"""
    assert quota_service.should_weaken_prompt(_quota(remaining=0, reject_streak=WEAKEN_AFTER_DAYS), TODAY) is True


def test_should_weaken_false_when_has_remaining() -> None:
    """还有额度：永不降级。"""
    assert quota_service.should_weaken_prompt(_quota(remaining=1, reject_streak=9), TODAY) is False


def test_should_weaken_false_below_threshold() -> None:
    """无额度但未达阈值：不降级。"""
    assert quota_service.should_weaken_prompt(_quota(remaining=0, reject_streak=WEAKEN_AFTER_DAYS - 1), TODAY) is False


async def test_get_quota_without_row_is_zero_and_not_weakened(session_factory) -> None:
    """从未有额度行：视图为全零且不降级（新农户应正常看到授权入口）。"""
    async with session_factory() as db:
        view = await get_quota(db, USER, today=TODAY)

    assert view == QuotaView(remaining=0, grantedTotal=0, consumedTotal=0, weakened=False)


# ---- 四、退还 ----


async def test_refund_restores_after_consume(engine, session_factory) -> None:
    """消费成功后退还：remaining 与 consumed_total 回到调用前的值。"""
    await _seed_quota(engine, granted_total=1, consumed_total=0, remaining=1)

    async with session_factory() as db:
        assert await try_consume(db, USER) is True
    async with session_factory() as db:
        assert await refund(db, USER) is True

    row = await _read(engine)
    assert row is not None
    assert row.remaining == 1 and row.consumed_total == 0


async def test_refund_without_consume_is_noop(engine, session_factory) -> None:
    """无可退记录：退还无操作，remaining 不变成负数、不被凭空加一。"""
    await _seed_quota(engine, granted_total=1, consumed_total=0, remaining=1)

    async with session_factory() as db:
        assert await refund(db, USER) is False

    row = await _read(engine)
    assert row is not None
    assert row.remaining == 1 and row.consumed_total == 0


# ---- 五、接口归属与拒绝计数 ----


def test_quota_and_grant_contract_exclude_user_id() -> None:
    """额度是每户资产：两个契约的字段集合都不含 user_id。"""
    assert "user_id" not in QuotaView.model_fields
    assert "user_id" not in GrantRequest.model_fields


def test_grant_request_accepts_granted_flag() -> None:
    req = GrantRequest(granted=False, scene="after_snapshot")
    assert req.granted is False and req.scene == "after_snapshot"


async def test_reject_increments_streak_without_touching_quota(engine, session_factory) -> None:
    """granted=false：额度不变、reject_streak 加一（接口路径的回传结果）。"""
    await _seed_quota(engine, granted_total=3, consumed_total=3, remaining=0, reject_streak=1)

    async with session_factory() as db:
        view = await grant(db, USER, GrantRequest(granted=False))

    assert view.remaining == 0 and view.grantedTotal == 3
    row = await _read(engine)
    assert row is not None
    assert row.reject_streak == 2 and row.remaining == 0


async def test_reject_without_row_creates_streak_one(engine, session_factory) -> None:
    """首次即拒绝：建行，reject_streak=1、remaining=0（不做无来源的授权）。"""
    async with session_factory() as db:
        await grant(db, USER, GrantRequest(granted=False))

    row = await _read(engine)
    assert row is not None
    assert row.reject_streak == 1 and row.remaining == 0 and row.granted_total == 0


def test_quota_endpoints_registered() -> None:
    """两个路由挂在 /api/subscription 下（注册存在性，避免被漏挂）。

    `app.routes` 顶层是惰性 `_IncludedRouter` 包装，直接取 `route.path` 拿不到子路由；
    改从 OpenAPI 文档取已展开的路径集合。
    """
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/subscription/quota" in paths
    assert "/api/subscription/grant" in paths


def _login_headers(client, code: str) -> dict:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['token']}"}


def test_api_grant_false_keeps_quota_and_api_isolation(client) -> None:
    """接口路径：拒绝后额度不变；另一用户令牌读自己的额度（隔离）。

    两个代码各自换一个独立用户：A 授权成功获得 1 份额度，B 查询到的仍是自己的零额度，
    证明额度按令牌归属读取、不可跨用户读取。
    """
    headers_a = _login_headers(client, "t311-a")
    headers_b = _login_headers(client, "t311-b")

    # A 先授权成功 → remaining=1
    resp = client.post("/api/subscription/grant", json={"granted": True}, headers=headers_a)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["remaining"] == 1

    # B 查询自己的额度 → 与自己一致（零），与 A 隔离
    resp_b = client.get("/api/subscription/quota", headers=headers_b)
    assert resp_b.status_code == 200, resp_b.text
    data_b = resp_b.json()["data"]
    assert data_b["remaining"] == 0 and data_b["grantedTotal"] == 0

    # A 拒绝一次 → 额度不变（remaining 仍为 1）
    resp_rej = client.post("/api/subscription/grant", json={"granted": False}, headers=headers_a)
    assert resp_rej.status_code == 200, resp_rej.text
    assert resp_rej.json()["data"]["remaining"] == 1

    # A 的额度仍可查得，计数未被拒绝影响
    resp_a = client.get("/api/subscription/quota", headers=headers_a)
    assert resp_a.json()["data"]["remaining"] == 1


def test_api_requires_auth(client) -> None:
    """无令牌：额度接口返回 401。"""
    assert client.get("/api/subscription/quota").status_code == 401
