"""T3-15 验收：处方卡台账与执行回执（技术方案 13.1 / 产品说明书 4.5.2、4.3.3）。

本用例把任务文件点名的断言逐条钉死：

1. **入台账幂等**：同一处方卡连续调用 `save_prescription_to_ledger` 两次，台账表只有 1 条
   记录且两次返回同一 `entry_id`；处方卡的 `ledger_saved` 被置为 `True`。
2. **台账自包含**：条目里存有药剂名与 `safety_interval_days` 的副本（不引用 `knowledge_id`）。
3. **勾选与取消**：`executed=True` 后 `executed_at` 非空；再 `executed=False` 断言
   `executed=False` 且 `executed_at is None`；被抑制条目的勾选抛 `InvalidStateError`。
4. **执行率**：10 条展示条目其中 4 条勾选、另有 5 条被抑制 → `shown=10`、`rate=0.4`；
   不传 `process_id` 时返回按流程分组的字典。
5. **归属校验**：另一用户读不到目标数据（返回 `None` / 影响 0 行），不修改目标数据。

需要真实 PostgreSQL：全部断言都落在 `ledger_entry` / `prescription_card` / `prompt_snapshot` /
`prompt_item` 的真实读写上，唯一约束与归属过滤都是 PG 语义。
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import InvalidStateError, NotFound
from app.db.base import new_id
from app.models import Consultation, LedgerEntry, PrescriptionCard, PromptItem, PromptSnapshot
from app.models.enums import ConsultStatus
from app.services.ledger_service import (
    execution_rate,
    ignored_streak,
    list_ledger,
    save_prescription_to_ledger,
    set_item_executed,
)

pytestmark = pytest.mark.usefixtures("requires_postgres")

USER = "user-t315"
OTHER = "user-t315-other"
PROCESS = "rice"
TODAY = date(2026, 6, 1)


def _consultation(*, user_id: str = USER) -> Consultation:
    return Consultation(
        id=new_id("sess"),
        farm_entry_id=None,
        process_id=PROCESS,
        process_locked=False,
        title="台账测试问诊",
        object_label="水稻·3亩",
        topic_summary="药剂使用",
        stage="followup",
        rounds=1,
        status=ConsultStatus.FOLLOWUP.value,
        triage_snapshot={},
        slots_snapshot={},
        user_id=user_id,
    )


def _card(
    *,
    consultation_id: str,
    user_id: str = USER,
    process_id: str | None = PROCESS,
    diagnosis_summary: str | None = "稻飞虱防治",
    safety_interval_days: int | None = 14,
    actions: list[dict] | None = None,
    pesticide_items: list[dict] | None = None,
    followup_plan: dict | None = None,
    card_id: str | None = None,
    created_at: datetime | None = None,
) -> PrescriptionCard:
    """构造处方卡。`created_at` 显式给值以便断言 `entry_date`（否则由 server_default 兜底）。"""
    card = PrescriptionCard(
        id=card_id or new_id("card"),
        consultation_id=consultation_id,
        user_id=user_id,
        process_id=process_id,
        diagnosis_summary=diagnosis_summary,
        safety_interval_days=safety_interval_days,
        actions=actions if actions is not None else [{"order": 1, "action": "喷施吡虫啉"}],
        pesticide_items=pesticide_items if pesticide_items is not None else [{"name": "吡虫啉", "dosage": "10g"}],
        followup_plan=followup_plan if followup_plan is not None else {"tasks": [{"due_offset_days": 5}]},
    )
    if created_at is not None:
        card.created_at = created_at
    return card


def _snapshot(*, user_id: str = USER, process_id: str | None = PROCESS, day: date = TODAY) -> PromptSnapshot:
    return PromptSnapshot(
        id=new_id("snap"),
        user_id=user_id,
        farm_entry_id=None,
        snapshot_date=day,
        generated_at=datetime.now(UTC),
        env_source="none",
        revision=1,
        process_id=process_id,
    )


def _item(*, snapshot_id: str, user_id: str = USER, suppressed: bool = False, executed: bool = False) -> PromptItem:
    return PromptItem(
        id=new_id("item"),
        user_id=user_id,
        snapshot_id=snapshot_id,
        source_kind="calendar",
        severity="notice",
        callout="水稻·3亩",
        fact="当前处于分蘖期",
        impact="不及时处理会影响产量",
        action="按处方施药",
        rule_id=new_id("rule"),
        facts_provenance={},
        priority_score=1,
        suppressed=suppressed,
        executed=executed,
    )


async def _persist(engine, *objects) -> None:
    """逐对象 `add` + `flush` 落库，保证外键依赖顺序（表间无 ORM relationship）。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


async def _count(engine, model) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _seed_card(engine, **card_kwargs) -> PrescriptionCard:
    consult = _consultation()
    card = _card(consultation_id=consult.id, **card_kwargs)
    await _persist(engine, consult, card)
    return card


# --------------------------------------------------------------------------
# 一、入台账与幂等
# --------------------------------------------------------------------------


async def test_save_twice_is_idempotent_and_marks_saved(engine) -> None:
    """同一处方卡连续入台账两次 → 只落 1 条、返回同一 entry_id、`ledger_saved=True`。"""
    card = await _seed_card(engine, created_at=datetime(2026, 6, 1, 8, tzinfo=UTC))

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        first = await save_prescription_to_ledger(db, USER, card.id)
        second = await save_prescription_to_ledger(db, USER, card.id)

    assert first.id == second.id
    assert await _count(engine, LedgerEntry) == 1

    async with factory() as db:
        saved = await db.get(PrescriptionCard, card.id)
        assert saved is not None and saved.ledger_saved is True


async def test_entry_copies_card_fields_and_is_self_contained(engine) -> None:
    """条目字段从处方卡复制：药剂名、安全间隔期、动作、标题、流程、农事日期。"""
    card = await _seed_card(
        engine,
        created_at=datetime(2026, 6, 1, 8, tzinfo=UTC),
        diagnosis_summary="稻飞虱防治",
        safety_interval_days=14,
        actions=[{"order": 1, "action": "喷施吡虫啉"}],
        pesticide_items=[{"name": "吡虫啉", "dosage": "10g"}],
    )

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        entry = await save_prescription_to_ledger(db, USER, card.id)

    assert entry.entry_date == date(2026, 6, 1)
    assert entry.process_id == PROCESS
    assert entry.title == "稻飞虱防治"
    assert entry.safety_interval_days == 14
    assert entry.actions == [{"order": 1, "action": "喷施吡虫啉"}]
    assert entry.pesticides == [{"name": "吡虫啉", "dosage": "10g"}]
    assert entry.source_type == "prescription"
    assert entry.source_id == card.id
    assert entry.followup_needed is True


async def test_entry_followup_needed_false_without_plan(engine) -> None:
    """无回访计划的处方（如转人工降级），台账条目标记不需要回访。"""
    card = await _seed_card(engine, created_at=datetime(2026, 6, 1, tzinfo=UTC), followup_plan={})

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        entry = await save_prescription_to_ledger(db, USER, card.id)

    assert entry.followup_needed is False


async def test_save_unknown_card_raises_not_found(engine) -> None:
    """处方卡不存在 → `NotFound`（不区分"不存在"与"不属于你"）。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(NotFound):
            await save_prescription_to_ledger(db, USER, "card-does-not-exist")


async def test_save_other_users_card_is_not_found_and_does_not_write(engine) -> None:
    """他人处方卡 → 404 且不写台账（归属校验在仓储内）。"""
    card = await _seed_card(engine, created_at=datetime(2026, 6, 1, tzinfo=UTC))

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(NotFound):
            await save_prescription_to_ledger(db, OTHER, card.id)

    assert await _count(engine, LedgerEntry) == 0


# --------------------------------------------------------------------------
# 二、台账列表
# --------------------------------------------------------------------------


async def test_list_ledger_desc_and_process_filter(engine) -> None:
    """列表按 entry_date 倒序；`process_id` 给定时只取该流程。"""
    c1 = _consultation()
    c2 = _consultation()
    card_early = _card(
        consultation_id=c1.id, card_id="card-early", created_at=datetime(2026, 5, 1, tzinfo=UTC)
    )
    card_late = _card(
        consultation_id=c2.id, card_id="card-late", created_at=datetime(2026, 6, 1, tzinfo=UTC), process_id="tea"
    )
    await _persist(engine, c1, c2, card_early, card_late)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await save_prescription_to_ledger(db, USER, card_early.id)
        await save_prescription_to_ledger(db, USER, card_late.id)

    async with factory() as db:
        all_entries = await list_ledger(db, USER)
        rice_entries = await list_ledger(db, USER, process_id=PROCESS)

    assert [e.source_id for e in all_entries] == ["card-late", "card-early"]
    assert [e.source_id for e in rice_entries] == ["card-early"]


# --------------------------------------------------------------------------
# 三、执行勾选与取消
# --------------------------------------------------------------------------


async def _seed_item(engine, *, suppressed: bool = False) -> PromptItem:
    snap = _snapshot()
    item = _item(snapshot_id=snap.id, suppressed=suppressed)
    await _persist(engine, snap, item)
    return item


async def test_execute_sets_timestamp_and_cancel_clears_it(engine) -> None:
    """勾选后 `executed_at` 非空；取消后 `executed=False` 且 `executed_at is None`。"""
    item = await _seed_item(engine)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        checked = await set_item_executed(db, USER, item.id, True)
    assert checked.executed is True
    assert checked.executed_at is not None

    async with factory() as db:
        cancelled = await set_item_executed(db, USER, item.id, False)
    assert cancelled.executed is False
    assert cancelled.executed_at is None


async def test_execute_suppressed_item_raises_invalid_state(engine) -> None:
    """被抑制条目从未展示，勾选请求抛 `InvalidStateError`（400）。"""
    item = await _seed_item(engine, suppressed=True)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(InvalidStateError):
            await set_item_executed(db, USER, item.id, True)

    async with factory() as db:
        stored = await db.get(PromptItem, item.id)
        assert stored is not None and stored.executed is False


async def test_execute_unknown_item_raises_not_found(engine) -> None:
    """条目不存在 → `NotFound`。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(NotFound):
            await set_item_executed(db, USER, "item-does-not-exist", True)


async def test_execute_other_users_item_is_not_found(engine) -> None:
    """他人条目 → 404 且不修改目标数据。"""
    item = await _seed_item(engine)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(NotFound):
            await set_item_executed(db, OTHER, item.id, True)

    async with factory() as db:
        stored = await db.get(PromptItem, item.id)
        assert stored is not None and stored.executed is False


# --------------------------------------------------------------------------
# 四、执行率与连续未勾选
# --------------------------------------------------------------------------


async def test_execution_rate_counts_shown_and_executed_excluding_suppressed(engine) -> None:
    """10 条展示条目（4 勾选）+ 5 条被抑制 → shown=10、executed=4、rate=0.4。"""
    snap = _snapshot()
    await _persist(engine, snap)

    items = [_item(snapshot_id=snap.id, executed=i < 4) for i in range(10)]
    items += [_item(snapshot_id=snap.id, suppressed=True, executed=False) for _ in range(5)]
    await _persist(engine, *items)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        rate = await execution_rate(db, TODAY, PROCESS)

    assert rate["shown"] == 10
    assert rate["executed"] == 4
    assert rate["rate"] == pytest.approx(0.4)


async def test_execution_rate_groups_by_process_when_not_specified(engine) -> None:
    """不传 `process_id` → 返回按流程分组的字典。"""
    snap_rice = _snapshot(process_id="rice", day=date(2026, 6, 2))
    snap_tea = _snapshot(process_id="tea", day=date(2026, 6, 1))
    await _persist(engine, snap_rice, snap_tea)
    await _persist(
        engine,
        _item(snapshot_id=snap_rice.id, executed=True),
        _item(snapshot_id=snap_rice.id, executed=False),
        _item(snapshot_id=snap_tea.id, executed=False),
    )

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        grouped = await execution_rate(db, TODAY)

    assert grouped["rice"]["shown"] == 2
    assert grouped["rice"]["executed"] == 1
    assert grouped["rice"]["rate"] == pytest.approx(0.5)
    assert grouped["tea"]["shown"] == 1
    assert grouped["tea"]["rate"] == pytest.approx(0.0)


async def test_execution_rate_zero_denominator_is_zero(engine) -> None:
    """无展示条目时 `rate` 取 0.0，避免除零。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        rate = await execution_rate(db, TODAY, PROCESS)

    assert rate == {"shown": 0, "executed": 0, "rate": 0.0}


async def test_ignored_streak_counts_consecutive_unexecuted(engine) -> None:
    """连续未勾选：从最近展示条目向前数，遇到已勾选即停，被抑制条目不计入。"""
    newer = _snapshot(day=date(2026, 6, 2))
    older = _snapshot(day=date(2026, 6, 1))
    await _persist(engine, newer, older)
    await _persist(
        engine,
        _item(snapshot_id=newer.id, executed=False),
        _item(snapshot_id=newer.id, executed=False),
        _item(snapshot_id=newer.id, suppressed=True, executed=False),
        _item(snapshot_id=older.id, executed=True),
    )

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        streak = await ignored_streak(db, USER)

    assert streak == 2
