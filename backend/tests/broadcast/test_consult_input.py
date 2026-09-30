"""T3-04 验收：问诊与回访信号采集。

覆盖验收标准里的五个点：两个来源各自产出、观察期文案不得问"效果"、到期条目带
`task_id`、去重（同日快照已含同 `rule_id` 时第二次返回 0）、互斥（已登记回访的问诊
不再由观察期来源产出）、以及 `severity` 按 `kind` 映射。

需要真实 PostgreSQL 的用例走 `engine` 会话级迁移库（`consultation` 表由 0010 迁移建出）；
`build_followup_prompt` / `build_followup_rule` 是纯函数，用内存构造对象即可。

两处测试夹具上的注意点：

1. 表之间只有外键、没有 ORM `relationship`，单次 flush 不保证按外键顺序落库（`followup_task`
   可能先于 `prescription_card` 插入而撞外键）。故 `_persist` 逐对象 `add` + `flush`，逐级落库。
2. `update_time` 是观察期判定的时间轴，用例把它回拨 `days_ago` 天；`TODAY` 取真实当天的
   日期，使"回拨 N 天"与 `days_ago(today)` 的整数天数对齐。
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.broadcast.inputs.consult import (
    build_followup_prompt,
    build_followup_rule,
    collect_consult_signals,
)
from app.db.base import new_id
from app.models import Consultation, FollowupTask, PrescriptionCard
from app.models.enums import ConsultStatus, FollowupKind, FollowupStatus

pytestmark = pytest.mark.usefixtures("requires_postgres")

USER = "user-t304"
USER_B = "user-t304-b"
TODAY = datetime.now(UTC).date()


def _consultation(
    *,
    user_id: str = USER,
    status: str = ConsultStatus.OBSERVING.value,
    process_id: str = "rice",
    days_ago: int = 3,
    topic_summary: str = "叶片上的褐色斑点",
    object_label: str = "水稻·3亩",
    consult_id: str | None = None,
) -> Consultation:
    """构造一条问诊；`update_time` 回拨 `days_ago` 天以驱动观察期判定。"""
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return Consultation(
        id=consult_id or new_id("sess"),
        farm_entry_id="crop-1",
        process_id=process_id,
        process_locked=False,
        title="叶片斑点问诊",
        object_label=object_label,
        topic_summary=topic_summary,
        stage="observe",
        rounds=1,
        status=status,
        triage_snapshot={},
        slots_snapshot={},
        user_id=user_id,
        create_time=when,
        update_time=when,
    )


def _card(*, user_id: str = USER, consultation_id: str, card_id: str | None = None) -> PrescriptionCard:
    return PrescriptionCard(id=card_id or new_id("card"), consultation_id=consultation_id, user_id=user_id)


def _task(
    *,
    user_id: str = USER,
    consultation_id: str,
    prescription_card_id: str,
    kind: str = FollowupKind.EFFECT_CHECK.value,
    content_rule: str | None = "weed.damage_evidence_checklist",
    ask: str = "药后效果如何，草有没有发黄枯死",
    object_label: str = "水稻·3亩",
    due_offset_days: int = 0,
    status: str = FollowupStatus.PENDING.value,
    task_id: str | None = None,
) -> FollowupTask:
    return FollowupTask(
        id=task_id or new_id("fu"),
        consultation_id=consultation_id,
        prescription_card_id=prescription_card_id,
        kind=kind,
        object_label=object_label,
        ask=ask,
        content_rule=content_rule,
        due_at=datetime.now(UTC) + timedelta(days=due_offset_days),
        status=status,
        user_id=user_id,
    )


async def _persist(engine, *objects) -> None:
    """逐个 `add` + `flush` 落库，保证外键依赖顺序（表间无 ORM relationship）。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


async def _collect(engine, user_id: str = USER, *, existing_rule_ids=(), today: date = TODAY):
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        return await collect_consult_signals(db, user_id, today, existing_rule_ids=existing_rule_ids)


@pytest.fixture()
async def seeded(engine):
    """建一条观察期问诊 + 其处方卡 + 一条到期回访任务，返回三者的 id。"""
    consult = _consultation(status=ConsultStatus.FOLLOWUP.value)
    card = _card(consultation_id=consult.id)
    task = _task(consultation_id=consult.id, prescription_card_id=card.id)
    await _persist(engine, consult, card, task)
    return {"consultation_id": consult.id, "card_id": card.id, "task_id": task.id}


# ---- 一、观察期来源 ----


async def test_observing_consultation_beyond_threshold_yields_followup(engine) -> None:
    """超过观察期阈值（默认 2 天）的观察期问诊 → 产出 followup 类条目，fact 含主题摘要。"""
    await _persist(engine, _consultation(days_ago=3, topic_summary="叶片上的褐色斑点"))

    items = await _collect(engine)

    observing = [i for i in items if i.rule_id.startswith("followup.observing.")]
    assert len(observing) == 1
    assert observing[0].source_kind == "followup"
    assert "叶片上的褐色斑点" in observing[0].fact


async def test_observing_consultation_within_threshold_produces_nothing(engine) -> None:
    """未满观察期阈值（当天）的观察期问诊不产出条目。"""
    await _persist(engine, _consultation(days_ago=0))

    assert await _collect(engine) == []


async def test_resolved_consultation_is_ignored(engine) -> None:
    """非观察期状态（如已解决）不问天数，一律不产出观察期条目。"""
    await _persist(engine, _consultation(status=ConsultStatus.RESOLVED.value, days_ago=10))

    assert await _collect(engine) == []


def test_observing_prompt_never_asks_about_effect() -> None:
    """观察期问诊无处方，说"效果如何"等于告诉农户系统记错了。"""
    c = _consultation(days_ago=3)
    item = build_followup_prompt(c, TODAY)
    assert "效果" not in item.fact
    assert "效果" not in item.action
    assert item.severity == "reminder"
    assert item.callout == "水稻·3亩"


async def test_other_user_consultation_is_invisible(engine) -> None:
    """跨用户不可见：属于 B 的问诊不进入 A 的信号。"""
    await _persist(engine, _consultation(user_id=USER_B, days_ago=5))

    assert await _collect(engine, USER) == []


# ---- 二、到期回访来源 ----


async def test_due_task_rule_id_comes_from_content_rule(engine) -> None:
    """到期 pending 任务 → rule_id 以 content_rule 为前缀（追加 task_id 保证逐任务唯一）。"""
    consult = _consultation(status=ConsultStatus.FOLLOWUP.value)
    card = _card(consultation_id=consult.id)
    task = _task(
        consultation_id=consult.id,
        prescription_card_id=card.id,
        content_rule="weed.damage_evidence_checklist",
    )
    await _persist(engine, consult, card, task)

    items = await _collect(engine)

    due = [i for i in items if i.rule_id.startswith("weed.damage_evidence_checklist:")]
    assert len(due) == 1
    assert due[0].rule_id == f"weed.damage_evidence_checklist:{task.id}"


async def test_due_item_carries_task_id_provenance(engine, seeded) -> None:
    """到期条目必须携带 task_id，供前端在首页点"回填"时定位任务。"""
    items = await _collect(engine)
    due = [i for i in items if i.facts_provenance.get("task_id") == seeded["task_id"]]
    assert len(due) == 1


async def test_future_task_is_not_due(engine) -> None:
    """due_at 在未来（+3 天）的任务不算到期。"""
    consult = _consultation(status=ConsultStatus.FOLLOWUP.value)
    card = _card(consultation_id=consult.id)
    task = _task(consultation_id=consult.id, prescription_card_id=card.id, due_offset_days=3)
    await _persist(engine, consult, card, task)

    assert await _collect(engine) == []


async def test_done_task_is_not_due(engine) -> None:
    """已回填（status=done）的任务不再产出到期条目。"""
    consult = _consultation(status=ConsultStatus.FOLLOWUP.value)
    card = _card(consultation_id=consult.id)
    task = _task(
        consultation_id=consult.id,
        prescription_card_id=card.id,
        status=FollowupStatus.DONE.value,
    )
    await _persist(engine, consult, card, task)

    assert await _collect(engine) == []


# ---- 三、去重 ----


async def test_second_run_with_existing_rule_ids_returns_zero(engine, seeded) -> None:
    """同日快照已含同 rule_id 时，重跑不再产出该条（回访任务回填前会一直 pending）。"""
    first = await _collect(engine)
    assert first, "首轮应产出条目"
    existing = {i.rule_id for i in first}

    second = await _collect(engine, existing_rule_ids=existing)

    assert second == []


async def test_two_distinct_tasks_produce_two_items(engine) -> None:
    """同一问诊的两条不同回访任务各自成条（rule_id 带 task_id，不被折叠）。"""
    consult = _consultation(status=ConsultStatus.FOLLOWUP.value)
    card = _card(consultation_id=consult.id)
    t1 = _task(consultation_id=consult.id, prescription_card_id=card.id, content_rule="weed.damage_evidence_checklist")
    t2 = _task(consultation_id=consult.id, prescription_card_id=card.id, content_rule="weed.next_crop_check")
    await _persist(engine, consult, card, t1, t2)

    items = await _collect(engine)

    assert len(items) == 2
    assert len({i.rule_id for i in items}) == 2


# ---- 四、两个来源互斥 ----


async def test_consultation_with_registered_task_yields_single_item(engine, seeded) -> None:
    """既有处方卡又已登记回访的问诊：观察期来源让位，总条目数为 1（仅回访来源）。"""
    items = await _collect(engine)

    assert len(items) == 1
    assert items[0].rule_id.startswith("weed.damage_evidence_checklist:")
    assert not any(i.rule_id.startswith("followup.observing.") for i in items)


# ---- 五、severity 按 kind 映射 ----


def test_severity_preserve_scene_is_urgent() -> None:
    """药害固定现场提示错过表现期即失去鉴别依据，判 urgent。"""
    task = _task(
        consultation_id="c1",
        prescription_card_id="p1",
        kind=FollowupKind.PRESERVE_SCENE.value,
        content_rule="weed.damage_evidence_checklist",
    )
    assert build_followup_rule(task).severity == "urgent"


def test_severity_effect_check_is_notice() -> None:
    """普通效果核查默认 notice。"""
    task = _task(
        consultation_id="c1",
        prescription_card_id="p1",
        kind=FollowupKind.EFFECT_CHECK.value,
    )
    assert build_followup_rule(task).severity == "notice"


def test_severity_unknown_kind_falls_back_to_notice() -> None:
    """`compliance_check` 等未登记 kind 落到默认 notice，不误判为紧急。"""
    task = _task(
        consultation_id="c1",
        prescription_card_id="p1",
        kind=FollowupKind.COMPLIANCE_CHECK.value,
    )
    assert build_followup_rule(task).severity == "notice"


def test_followup_rule_callout_and_fact_from_task() -> None:
    """到期条目取任务的 object_label 与 ask。"""
    task = _task(
        consultation_id="c1",
        prescription_card_id="p1",
        object_label="辣椒·2亩",
        ask="三天后叶片恢复情况",
    )
    item = build_followup_rule(task)
    assert item.callout == "辣椒·2亩"
    assert item.fact == "三天后叶片恢复情况"
    assert item.source_kind == "followup"
