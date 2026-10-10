"""T3-09 验收：回访调度服务（技术方案 11.10）。

本用例把任务文件点名的断言逐条钉死：

1. **七条流程的回访时点**：各流程按 `followup_policy.triggers` 产出对应条数的任务，
   `due_at` 落在"相对当天的绝对时点"。天数一律来自配置，本文件不硬编码节奏——只在
   断言里比对"配置给出的天数 + 基准日"。
2. **两处特例**：水稻的生育期窗口（农事历给节点日、取节点前一天，缺历回落到配置天数）、
   茶取 `safety_interval_days` 与 `next_pick_date` 的较晚者。
3. **杂草药害分支**：`branch="herbicide_damage"` 产出恰好 1 条 `preserve_scene`（T+0），
   不叠加常规回访。
4. **T+0 立即投递**：`due_at <= now` 的任务在登记时即刻产生 `followup.due` 事件，
   不等次日扫描（`event_outbox.available_at <= now`）。
5. **到期扫描**：`list_due` / `dispatch_due` 只取昨天与今天、`status=pending`，不取明天、
   不取已回填。
6. **外键**：`prescription_card_id` 指向不存在的卡片必须被外键拦下。
7. **无硬编码天数**：服务源码不得出现配置字段名（验收的 grep 条件）。

需要真实 PostgreSQL：全部断言都落在 `followup_task` / `event_outbox` 两张表的真实读写上，
且外键与唯一约束是 PG 语义。日期用固定基准 `TODAY`，断言与运行时钟无关（唯一的例外是
T+0 立即投递用例，它必须用真实当天才能验证"到期即投"）。
"""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from asyncpg.exceptions import ForeignKeyViolationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import FollowupPlan
from app.agent.contracts import FollowupTask as FollowupTaskContract
from app.agent.state import ProcessId
from app.db.base import new_id
from app.events.handlers import EVENT_FOLLOWUP_DUE
from app.models import Consultation, EventOutbox, FollowupTask, PrescriptionCard
from app.models.enums import ConsultStatus, FollowupStatus
from app.repositories import consult_repo
from app.services import followup_service
from app.services.followup_service import build_followup_plan, dispatch_due, list_due, register_from_prescription

pytestmark = pytest.mark.usefixtures("requires_postgres")

USER = "user-t309"
# 固定基准日：所有相对天数的断言都与它相减，不依赖运行时钟。
TODAY = date(2026, 5, 20)


def _at(day: date) -> datetime:
    """日期 → 当日零点的 UTC 时点（与服务内换算口径一致，便于逐条比对 `due_at`）。"""
    return datetime.combine(day, time.min, tzinfo=UTC)


def _consultation(
    *,
    user_id: str = USER,
    process_id: str = "rice",
    object_label: str = "水稻·3亩",
    consult_id: str | None = None,
) -> Consultation:
    return Consultation(
        id=consult_id or new_id("sess"),
        process_id=process_id,
        process_locked=False,
        title="回访登记测试问诊",
        object_label=object_label,
        topic_summary="叶片问题",
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
    followup_plan: dict | None = None,
    safety_interval_days: int | None = None,
    next_pick_date: date | None = None,
    card_id: str | None = None,
) -> PrescriptionCard:
    return PrescriptionCard(
        id=card_id or new_id("card"),
        consultation_id=consultation_id,
        user_id=user_id,
        followup_plan=followup_plan,
        safety_interval_days=safety_interval_days,
        next_pick_date=next_pick_date,
    )


def _orm_task(
    *,
    consultation_id: str,
    prescription_card_id: str,
    due_at: datetime,
    user_id: str = USER,
    kind: str = "effect_check",
    status: str = FollowupStatus.PENDING.value,
    ask: str = "药后效果如何",
    object_label: str = "水稻·3亩",
) -> FollowupTask:
    return FollowupTask(
        id=new_id("fu"),
        user_id=user_id,
        consultation_id=consultation_id,
        prescription_card_id=prescription_card_id,
        kind=kind,
        object_label=object_label,
        ask=ask,
        due_at=due_at,
        status=status,
    )


async def _persist(engine, *objects) -> None:
    """逐对象 `add` + `flush` 落库，保证外键依赖顺序（表间无 ORM relationship）。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


async def _seed_consult_card(engine, *, process_id: str, plan: FollowupPlan | None, **card_kwargs):
    consult = _consultation(process_id=process_id)
    card = _card(
        consultation_id=consult.id,
        followup_plan=None if plan is None else plan.model_dump(mode="json"),
        **card_kwargs,
    )
    await _persist(engine, consult, card)
    return consult, card


async def _register(
    engine,
    *,
    process_id: str,
    plan: FollowupPlan,
    today: date | None = TODAY,
    window_date: date | None = None,
    safety_interval_days: int | None = None,
    next_pick_date: date | None = None,
) -> list[FollowupTask]:
    """在一个会话内建问诊 + 处方卡并登记回访（模拟 `prescription.created` 消费者）。"""
    consult = _consultation(process_id=process_id)
    card = _card(
        consultation_id=consult.id,
        followup_plan=plan.model_dump(mode="json"),
        safety_interval_days=safety_interval_days,
        next_pick_date=next_pick_date,
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        db.add(consult)
        await db.flush()
        db.add(card)
        await db.flush()
        tasks = await register_from_prescription(db, USER, card, today=today, window_date=window_date)
        await db.commit()
        return list(tasks)


# --------------------------------------------------------------------------
# 一、七条流程的回访时点
# --------------------------------------------------------------------------


async def test_rice_falls_back_to_config_days_without_calendar(engine) -> None:
    """水稻（window_based）：无农事历节点时回落到配置天数 7 与 10。"""
    tasks = await _register(engine, process_id="rice", plan=build_followup_plan(ProcessId.RICE, None))

    assert len(tasks) == 2
    assert [t.due_at for t in tasks] == [_at(TODAY + timedelta(days=7)), _at(TODAY + timedelta(days=10))]


async def test_rice_window_date_shifts_due_to_day_before_node(engine) -> None:
    """水稻有农事历节点时，`due_at` 取节点前一天（在动作到来前提醒）。"""
    node = TODAY + timedelta(days=30)
    tasks = await _register(
        engine,
        process_id="rice",
        plan=build_followup_plan(ProcessId.RICE, None),
        window_date=node,
    )

    assert len(tasks) == 2
    assert {t.due_at for t in tasks} == {_at(node - timedelta(days=1))}


async def test_tea_due_is_later_of_safety_interval_and_next_pick(engine) -> None:
    """茶：`due_at` 取安全间隔期结束与下一轮采摘计划中较晚者（此处间隔期更晚）。"""
    tasks = await _register(
        engine,
        process_id="tea",
        plan=build_followup_plan(ProcessId.TEA, None),
        safety_interval_days=25,
        next_pick_date=TODAY + timedelta(days=20),
    )

    assert len(tasks) == 2
    assert {t.due_at for t in tasks} == {_at(TODAY + timedelta(days=25))}


async def test_tea_due_follows_next_pick_when_it_is_later(engine) -> None:
    """茶：下一轮采摘计划更晚时，回访跟着采摘计划走（农户那时才要决定是否采摘）。"""
    tasks = await _register(
        engine,
        process_id="tea",
        plan=build_followup_plan(ProcessId.TEA, None),
        safety_interval_days=10,
        next_pick_date=TODAY + timedelta(days=28),
    )

    assert {t.due_at for t in tasks} == {_at(TODAY + timedelta(days=28))}


async def test_chili_due_is_five_days(engine) -> None:
    """辣椒：药后固定取中位 5 天，单条。"""
    tasks = await _register(engine, process_id="chili", plan=build_followup_plan(ProcessId.CHILI, None))

    assert len(tasks) == 1
    assert tasks[0].due_at == _at(TODAY + timedelta(days=5))
    assert tasks[0].kind == "effect_check"


async def test_weed_two_followups_seven_and_fifteen(engine) -> None:
    """杂草：药后 7 天查药效、15 天查后茬，两条。"""
    tasks = await _register(engine, process_id="weed", plan=build_followup_plan(ProcessId.WEED, None))

    assert len(tasks) == 2
    assert [t.due_at for t in tasks] == [_at(TODAY + timedelta(days=7)), _at(TODAY + timedelta(days=15))]


async def test_pig_followups_come_from_legal_deadline_chain(engine) -> None:
    """养猪：按法定时限链取配置天数（上报 T+1、隔离 T+2），kind 含 compliance_check。"""
    tasks = await _register(engine, process_id="pig", plan=build_followup_plan(ProcessId.PIG, None))

    assert [t.due_at for t in tasks] == [_at(TODAY + timedelta(days=1)), _at(TODAY + timedelta(days=2))]
    assert tasks[0].kind == "compliance_check"
    assert tasks[1].kind == "effect_check"


async def test_general_followup_from_card_plan(engine) -> None:
    """通用：按处方卡 `followup_plan` 给出的观察时点（配置为 3 天）。"""
    tasks = await _register(engine, process_id="general", plan=build_followup_plan(ProcessId.GENERAL, None))

    assert len(tasks) == 1
    assert tasks[0].due_at == _at(TODAY + timedelta(days=3))


async def test_rapeseed_two_followups(engine) -> None:
    """油菜（window_based）：无农事历节点时回落到配置天数 5 与 7。"""
    tasks = await _register(engine, process_id="rapeseed", plan=build_followup_plan(ProcessId.RAPESEED, None))

    assert len(tasks) == 2
    assert [t.due_at for t in tasks] == [_at(TODAY + timedelta(days=5)), _at(TODAY + timedelta(days=7))]


# --------------------------------------------------------------------------
# 二、杂草药害分支（T+0 现场固定动作）
# --------------------------------------------------------------------------


async def test_herbicide_branch_registers_single_immediate_preserve_scene(engine) -> None:
    """药害分支：恰好 1 条 `preserve_scene`，T+0，带现场固定动作清单，不叠加常规回访。"""
    plan = build_followup_plan(ProcessId.WEED, "herbicide_damage")
    assert len(plan.tasks) == 1
    assert plan.tasks[0].due_offset_days == 0

    tasks = await _register(engine, process_id="weed", plan=plan)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.kind == "preserve_scene"
    assert task.content_rule == "weed.damage_evidence_checklist"
    assert task.due_at == _at(TODAY)
    # 不叠加常规回访：不应出现 7 天/15 天的效果核查
    assert task.kind != "effect_check"
    assert task.due_at not in {_at(TODAY + timedelta(days=7)), _at(TODAY + timedelta(days=15))}


# --------------------------------------------------------------------------
# 三、T+0 立即投递
# --------------------------------------------------------------------------


async def test_due_task_is_dispatched_immediately(engine) -> None:
    """登记一条 `due_at <= now` 的任务 → 立即出现 `followup.due` 且 `available_at <= now`。

    用真实当天作基准（`today=None`）：只有真实时钟才能验证"到期即投"，不等次日扫描。
    """
    plan = FollowupPlan(tasks=[FollowupTaskContract(due_offset_days=0, kind="effect_check", ask="现在看看效果")])

    await _register(engine, process_id="rice", plan=plan, today=None)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        rows = (
            (await db.execute(select(EventOutbox).where(EventOutbox.event_type == EVENT_FOLLOWUP_DUE))).scalars().all()
        )

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].available_at <= datetime.now(UTC), "立即事件必须马上可投递，不能等到次日扫描"


async def test_future_task_is_not_dispatched_immediately(engine) -> None:
    """未到期（T+5）的任务不产生立即事件，留待到期扫描。"""
    plan = FollowupPlan(tasks=[FollowupTaskContract(due_offset_days=5, kind="effect_check", ask="五天后再看")])

    await _register(engine, process_id="chili", plan=plan, today=None)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        rows = (await db.execute(select(EventOutbox))).scalars().all()

    assert rows == []


# --------------------------------------------------------------------------
# 四、到期扫描
# --------------------------------------------------------------------------


async def test_list_due_returns_yesterday_and_today_pending_only(engine) -> None:
    """3 条（昨/今/明）→ 只返回昨、今两条且均 pending；已回填的不返回。"""
    consult = _consultation()
    card = _card(consultation_id=consult.id)
    yesterday = _orm_task(
        consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY - timedelta(days=1))
    )
    today_task = _orm_task(consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY))
    tomorrow = _orm_task(
        consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY + timedelta(days=1))
    )
    done = _orm_task(
        consultation_id=consult.id,
        prescription_card_id=card.id,
        due_at=_at(TODAY - timedelta(days=1)),
        status=FollowupStatus.DONE.value,
    )
    await _persist(engine, consult, card, yesterday, today_task, tomorrow, done)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        due = await list_due(db, USER, TODAY)

    ids = [t.id for t in due]
    assert yesterday.id in ids
    assert today_task.id in ids
    assert tomorrow.id not in ids
    assert done.id not in ids
    assert all(t.status == FollowupStatus.PENDING.value for t in due)


async def test_dispatch_due_emits_for_each_due_task(engine) -> None:
    """每日扫描：为全部到期任务投 `followup.due`，返回条数，未到期的跳过。"""
    consult = _consultation()
    card = _card(consultation_id=consult.id)
    due_a = _orm_task(consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY - timedelta(days=1)))
    due_b = _orm_task(consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY))
    future = _orm_task(consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY + timedelta(days=3)))
    await _persist(engine, consult, card, due_a, due_b, future)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        count = await dispatch_due(db, TODAY)
        await db.commit()

        rows = (
            (await db.execute(select(EventOutbox).where(EventOutbox.event_type == EVENT_FOLLOWUP_DUE))).scalars().all()
        )

    assert count == 2
    assert {r.aggregate_id for r in rows} == {due_a.id, due_b.id}


async def test_complete_marks_task_done(engine) -> None:
    """`complete` 回填结果并关闭任务：`status=done`、写结果与关闭时点后不再到期。"""
    consult = _consultation()
    card = _card(consultation_id=consult.id)
    task = _orm_task(consultation_id=consult.id, prescription_card_id=card.id, due_at=_at(TODAY))
    await _persist(engine, consult, card, task)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        affected = await followup_service.complete(db, USER, task.id, result="improved", note="好转")
        await db.commit()

    assert affected == 1

    async with factory() as db:
        due = await list_due(db, USER, TODAY)
    assert task.id not in [t.id for t in due]


# --------------------------------------------------------------------------
# 五、契约边界与数据路径
# --------------------------------------------------------------------------


async def test_register_returns_empty_without_plan(engine) -> None:
    """无回访计划的处方（如转人工降级）不登记任何任务。"""
    consult, card = await _seed_consult_card(engine, process_id="general", plan=None)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        tasks = await register_from_prescription(db, USER, card, today=TODAY)
        await db.commit()

    assert tasks == []


async def test_register_defaults_ask_when_contract_omits_it(engine) -> None:
    """契约允许 `ask` 为空，而库列 NOT NULL：登记时补中性询问语，不留空。"""
    plan = FollowupPlan(tasks=[FollowupTaskContract(due_offset_days=2, kind="effect_check")])

    tasks = await _register(engine, process_id="general", plan=plan)

    assert len(tasks) == 1
    assert tasks[0].ask
    assert tasks[0].ask == followup_service._DEFAULT_ASK


async def test_reverse_lookup_by_consultation_is_owned(engine) -> None:
    """`prescription.created` 消费者的数据路径：按 `consultation_id` 反查处方卡再登记。

    反查强制归属：他人问诊查不到卡片，因此登记不会越权（此处验证取到卡片并完成登记）。
    """
    consult, card = await _seed_consult_card(
        engine, process_id="chili", plan=build_followup_plan(ProcessId.CHILI, None)
    )

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        found = await consult_repo.get_card_by_consultation(db, USER, consult.id)
        assert found is not None and found.id == card.id
        tasks = await register_from_prescription(db, USER, found, today=TODAY)
        await db.commit()

    assert len(tasks) == 1
    assert tasks[0].due_at == _at(TODAY + timedelta(days=5))


# --------------------------------------------------------------------------
# 六、外键约束
# --------------------------------------------------------------------------


async def test_foreign_key_violation_on_missing_card(engine) -> None:
    """`prescription_card_id` 指向不存在的卡片必须被外键拦下（12.7 高优先级外键）。"""
    consult = _consultation()
    await _persist(engine, consult)
    orphan = _orm_task(
        consultation_id=consult.id,
        prescription_card_id="card-does-not-exist",
        due_at=_at(TODAY),
    )

    with pytest.raises(IntegrityError) as exc:
        await _persist(engine, orphan)

    # SQLAlchemy 会把 asyncpg 异常再包一层（`orig` 是方言适配器异常，原始异常在
    # `__cause__` 链上），故沿链查找真正的 `ForeignKeyViolationError`。
    cause: BaseException | None = exc.value
    while cause is not None and not isinstance(cause, ForeignKeyViolationError):
        cause = cause.__cause__
    assert isinstance(cause, ForeignKeyViolationError), f"未在异常链中找到外键违例：{exc.value!r}"


# --------------------------------------------------------------------------
# 七、无硬编码天数（验收的 grep 条件）
# --------------------------------------------------------------------------


def test_service_source_has_no_hardcoded_days_field() -> None:
    """服务源码不得出现配置字段名 `after_days`：天数一律来自流程配置。"""
    src = Path(followup_service.__file__).read_text(encoding="utf-8")
    assert "after_days" not in src
