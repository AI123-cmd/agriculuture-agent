"""T3-08 验收：快照生成链路与幂等锁。

覆盖验收标准里的六个点：并发幂等（10 次并发只落一行、返回同一 `snapshot_id`）、同日重复
点击的差集合并（无新信号原样返回、有新信号 `revision` 自增且保留已勾选状态）、频次提示
（第 3 次起非空）、唯一约束兜底（绕过 Redis 仍只落一行）、文案安全校验（含未登记 "35℃"
的条目被丢弃且留审计）、以及接口契约（POST 含 `updateCompleted=true`、跨用户读不到）。

关于"绕过 Redis"：本仓库测试库不启用 Redis（`conftest` 置 `REDIS_URL=""`），`redis_lock`
在无 Redis 时本就是 no-op，因此并发正确性天然落在数据库唯一约束上。唯一约束兜底用例仍显式
把 `redis_lock` 替换成 no-op，把"锁不是幂等的必要条件"这一层次关系钉死在测试里。

数据访问一律走仓储（共用的 `session_factory` 夹具）；`PromptItem` 有 `snapshot_id` 外键，
故安全校验里的内存条目也补一个假 `snapshot_id`——它只被闸门读取，不落库。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.broadcast_service as broadcast_service
from app.broadcast.gate_check import RenderedItem, gate_check
from app.broadcast.snapshot import build_update_notice, generate_snapshot
from app.db.base import new_id
from app.models import Consultation, FollowupTask, GateAudit, PrescriptionCard, PromptItem, PromptSnapshot
from app.models.enums import ConsultStatus, FollowupKind, FollowupStatus
from app.repositories import snapshot_repo
from app.services.broadcast_service import BroadcastService

pytestmark = pytest.mark.usefixtures("requires_postgres")

USER = "user-t308"
USER_B = "user-t308-b"


def _consultation(*, user_id: str = USER, status: str = ConsultStatus.FOLLOWUP.value) -> Consultation:
    when = datetime.now(UTC) - timedelta(days=1)
    return Consultation(
        id=new_id("sess"),
        farm_entry_id=None,
        process_id="rice",
        process_locked=False,
        title="叶片斑点问诊",
        object_label="水稻·3亩",
        topic_summary="叶片上的褐色斑点",
        stage="observe",
        rounds=1,
        status=status,
        triage_snapshot={},
        slots_snapshot={},
        user_id=user_id,
        create_time=when,
        update_time=when,
    )


def _card(*, user_id: str = USER, consultation_id: str) -> PrescriptionCard:
    return PrescriptionCard(id=new_id("card"), consultation_id=consultation_id, user_id=user_id)


def _task(
    *,
    user_id: str = USER,
    consultation_id: str,
    prescription_card_id: str,
    content_rule: str = "weed.damage_evidence_checklist",
    due_offset_days: int = 0,
    status: str = FollowupStatus.PENDING.value,
) -> FollowupTask:
    return FollowupTask(
        id=new_id("fu"),
        consultation_id=consultation_id,
        prescription_card_id=prescription_card_id,
        kind=FollowupKind.EFFECT_CHECK.value,
        object_label="水稻·3亩",
        ask="药后效果如何，草有没有发黄枯死",
        content_rule=content_rule,
        due_at=datetime.now(UTC) + timedelta(days=due_offset_days),
        status=status,
        user_id=user_id,
    )


async def _persist(engine, *objects) -> None:
    """逐对象 `add` + `flush` 落库：表间只有外键、无 ORM relationship，需保证落库顺序。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


@asynccontextmanager
async def _noop_redis_lock(key: str, timeout: int = 30) -> AsyncIterator[None]:
    """`redis_lock` 的 no-op 替身：绕过 Redis，把并发正确性完全交给数据库唯一约束。"""
    yield


async def _generate(session_factory: async_sessionmaker[AsyncSession]) -> tuple[str, int]:
    """独立会话跑一次 `generate_snapshot`，返回 `(snapshot_id, revision)`。

    每次调用各开一个会话（对应"两个并发请求各自持有的连接"）；`generate_snapshot` 内部的
    `lock` 在测试环境是 no-op。
    """
    async with session_factory() as db:
        snapshot = await generate_snapshot(db, USER, None)
        return snapshot.id, snapshot.revision


async def _count_snapshots(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as db:
        stmt = (
            select(func.count())
            .select_from(PromptSnapshot)
            .where(
                PromptSnapshot.user_id == USER,
                PromptSnapshot.farm_entry_id.is_(None),
            )
        )
        return (await db.execute(stmt)).scalar_one()


async def _items(session_factory: async_sessionmaker[AsyncSession], snapshot_id: str) -> list[PromptItem]:
    async with session_factory() as db:
        return list(await snapshot_repo.list_items(db, USER, snapshot_id))


async def _mark_executed(session_factory: async_sessionmaker[AsyncSession], item_id: str) -> None:
    async with session_factory() as db:
        item = await db.get(PromptItem, item_id)
        assert item is not None
        item.executed = True
        await db.commit()


@pytest.fixture()
async def seeded(engine):
    """一条已登记回访的问诊（status=followup，观察期来源让位）+ 一条当天到期的回访任务。"""
    consult = _consultation()
    card = _card(consultation_id=consult.id)
    task = _task(consultation_id=consult.id, prescription_card_id=card.id)
    await _persist(engine, consult, card, task)
    return {"consultation_id": consult.id, "card_id": card.id, "task_id": task.id}


# ---- 一、并发幂等 ----


async def test_concurrent_generate_lands_single_snapshot(engine, session_factory, seeded) -> None:
    """10 次并发 `generate_snapshot`：只落 1 行，且所有调用返回同一 `snapshot_id`。"""
    results = await asyncio.gather(*[_generate(session_factory) for _ in range(10)])

    ids = {snapshot_id for snapshot_id, _ in results}
    assert len(ids) == 1, "并发下所有调用必须返回同一 snapshot_id"
    assert await _count_snapshots(session_factory) == 1


async def test_unique_constraint_is_the_guard_without_redis(engine, session_factory, seeded, monkeypatch) -> None:
    """把 `redis_lock` 换成 no-op（绕过 Redis）并发 10 次：仍只有 1 行、同一 id。

    锁只是"减少无效工作"，唯一的强一致保障来自 `(user_id, farm_entry_id, snapshot_date,
    revision)` 唯一约束与 `farm_entry_id IS NULL` 的部分唯一索引；并发插入撞约束后由
    `generate_snapshot` 回滚并取回已存在的那一份。
    """
    monkeypatch.setattr("app.broadcast.snapshot.redis_lock", _noop_redis_lock)

    results = await asyncio.gather(*[_generate(session_factory) for _ in range(10)])

    assert len({snapshot_id for snapshot_id, _ in results}) == 1
    assert await _count_snapshots(session_factory) == 1


# ---- 二、同日重复点击 ----


async def test_second_click_without_new_signal_returns_same_snapshot(engine, session_factory, seeded) -> None:
    """无新信号时第二次调用：同一 `snapshot_id`、`revision` 仍为 1、条目数与勾选状态不变。"""
    snapshot_id, revision = await _generate(session_factory)
    assert revision == 1

    items = await _items(session_factory, snapshot_id)
    assert len(items) == 1
    await _mark_executed(session_factory, items[0].id)

    again_id, again_revision = await _generate(session_factory)

    assert again_id == snapshot_id
    assert again_revision == 1
    after = await _items(session_factory, snapshot_id)
    assert len(after) == 1
    assert after[0].executed is True


async def test_new_due_task_merges_and_bumps_revision(engine, session_factory, seeded) -> None:
    """新到期的回访任务：`revision` 自增到 2、新条目并入，且原有已勾选条目 `executed` 仍为 True。"""
    snapshot_id, revision = await _generate(session_factory)
    assert revision == 1
    items = await _items(session_factory, snapshot_id)
    assert len(items) == 1
    await _mark_executed(session_factory, items[0].id)

    # 追加一条新到期任务（复用既有问诊与处方卡；rule_id 带 task_id，因此是新信号）。
    new_task = _task(
        consultation_id=seeded["consultation_id"],
        prescription_card_id=seeded["card_id"],
        content_rule="weed.next_crop_check",
    )
    await _persist(engine, new_task)

    merged_id, merged_revision = await _generate(session_factory)

    assert merged_id == snapshot_id
    assert merged_revision == 2
    after = await _items(session_factory, snapshot_id)
    assert len(after) == 2
    executed = {item.rule_id: item.executed for item in after}
    assert executed[f"weed.damage_evidence_checklist:{seeded['task_id']}"] is True
    assert executed[f"weed.next_crop_check:{new_task.id}"] is False


# ---- 三、频次提示 ----


def test_update_notice_is_none_below_threshold() -> None:
    """未达阈值（第 2 次）不提示。"""
    assert build_update_notice(2, True) is None


def test_update_notice_from_third_click() -> None:
    """第 3 次起提示，文案含"今日已更新"与次数。"""
    notice = build_update_notice(3, True)
    assert notice is not None
    assert "今日已更新" in notice
    assert "3" in notice


async def test_service_exposes_notice_on_third_click(engine, session_factory, seeded, monkeypatch) -> None:
    """接口层口径：连续 3 次生成时，第 3 次的 `updateNotice` 非空且含次数。

    测试环境无 Redis，`register_click` 恒返回 0，故在此把点击计数替身成自增计数器，
    验证 `BroadcastService.generate` 把计数正确接到 `build_update_notice` 上。
    """
    counter = {"n": 0}

    async def _counting_click(user_id: str, today) -> int:
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(broadcast_service, "register_click", _counting_click)
    service = BroadcastService()

    notices: list[str | None] = []
    for _ in range(3):
        async with session_factory() as db:
            result = await service.generate(db, USER, None)
        notices.append(result["updateNotice"])

    assert notices[0] is None
    assert notices[1] is None
    assert notices[2] is not None
    assert "今日已更新" in notices[2]
    assert "3" in notices[2]


# ---- 四、文案安全校验 ----


def _unregistered_item() -> PromptItem:
    """一条四段都不含单位数字的条目，供"文案里多出 35℃ 无来源"的构造使用。"""
    return PromptItem(
        id=new_id("item"),
        snapshot_id=new_id("snap"),
        user_id=USER,
        source_kind="followup",
        severity="notice",
        callout="水稻",
        fact="叶片发黄",
        impact="可能影响长势",
        action="建议查看",
        facts_provenance={},
        rule_id="followup.observing.rice",
        priority_score=10,
        suppressed=False,
        executed=False,
    )


async def test_gate_check_drops_text_with_unregistered_number(session_factory, monkeypatch) -> None:
    """文案含 "35℃" 但四段无登记：条目被丢弃，`gate_audit` 留一行 block 审计。

    `gate_check` 内部以 `GateAuditRepository()` 自管事务写审计，其构造默认绑定应用级
    `SessionLocal`（连接池）。本用例把它换成 NullPool 的测试工厂：审计照常落测试库，
    但连接不会跨事件循环被后续 `client` 用例的 TestClient portal 复用（否则报
    "attached to a different loop"）。
    """
    import app.repositories.gate_audit as gate_audit_repo

    monkeypatch.setattr(gate_audit_repo, "SessionLocal", session_factory)

    rendered = RenderedItem(item=_unregistered_item(), text="水稻：叶片发黄，当前气温 35℃ 偏高。")

    allowed = await gate_check(
        db=None,  # type: ignore[arg-type]  本函数不写业务表、不查库，db 仅用于签名一致
        user_id=USER,
        items=[rendered],
        process_id="general",
        config_version=1,
        today=datetime.now(UTC).date(),
    )

    assert allowed == []
    async with session_factory() as db:
        rows = list((await db.execute(select(GateAudit))).scalars().all())
    assert len(rows) == 1
    assert rows[0].decision == "block"
    assert rows[0].process_id == "general"


# ---- 五、接口契约 ----


def _login(client, code: str) -> tuple[str, dict]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200
    data = resp.json()["data"]
    return data["userId"], {"Authorization": f"Bearer {data['token']}"}


def test_post_snapshot_returns_update_completed(client) -> None:
    """POST /api/broadcast/snapshot 返回 200 且 `updateCompleted=true`。"""
    _, headers = _login(client, "snap-UA-0001")

    resp = client.post("/api/broadcast/snapshot", headers=headers)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["updateCompleted"] is True
    assert data["snapshot"]["snapshotId"]


def test_get_snapshot_is_scoped_to_owner(client) -> None:
    """另一用户的令牌 GET 读不到该用户的快照（只能拿到自己的那份）。"""
    _, headers_a = _login(client, "snap-UA-0001")
    a_snapshot = client.post("/api/broadcast/snapshot", headers=headers_a).json()["data"]["snapshot"]

    _, headers_b = _login(client, "snap-UB-0002")
    b_resp = client.get("/api/broadcast/snapshot", headers=headers_b)

    assert b_resp.status_code == 200
    b_snapshot = b_resp.json()["data"]["snapshot"]
    assert b_snapshot["snapshotId"] != a_snapshot["snapshotId"]
