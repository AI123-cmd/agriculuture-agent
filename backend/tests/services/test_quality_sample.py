"""T6-09 验收（三）：每日抽检的分层、幂等、样本不足标记与回退触发。

五组断言：

1. **按流程分层**：`build_daily_batch` 对七条流程各返回至少 3 例（14.5 原文的下限）。
2. **幂等**：同一天重复调用不新增样例行（`(run_date, prescription_card_id)` 唯一）。
3. **样本不足不补历史**：某条流程当日不足下限时返回实际条数并在 `insufficient` 里标记。
4. **批次字段**：每条记录含 `prescription_card_id` / `process_id`，`quality_label` 与
   `quality_labeled_at` 初始为 `None`。
5. **回退触发**：`should_rollback_prompt` 两日连续越线 True、仅一日 False；工单含两日样本
   清单与新旧提示词版本号。
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent.state import ProcessId
from app.core.config import get_settings
from app.db.base import new_id
from app.models import Consultation, EvalRun, PrescriptionCard
from app.models.eval_run import (
    KIND_QUALITY_SAMPLE,
    KIND_ROLLBACK_TICKET,
    QUALITY_LABEL_UNUSABLE,
    QUALITY_LABEL_USABLE,
)
from app.models.enums import ConsultStatus
from app.observability.metrics import should_rollback_prompt
from app.observability.sampling import collect_daily_metrics
from app.repositories.metrics import MetricsRepository
from app.services.quality_sample_service import QualitySampleService

pytestmark = pytest.mark.usefixtures("requires_postgres")

_USER = "user-t609-sample"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _today() -> date:
    return datetime.now(UTC).date()


def _day_start(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


async def _seed_cards(db, process_id: str, count: int, day: date) -> list[str]:
    """为某条流程造 count 张已开方的处方卡（含必填的所属问诊）。

    `created_at` 显式给到 `day` 的零点后一分钟：抽检按自然日过滤 `created_at`，
    只靠 `server_default=now()` 会让"昨天的批次"永远抽不到样本。
    """
    ids: list[str] = []
    for index in range(count):
        consult_id = new_id("sess")
        db.add(
            Consultation(
                id=consult_id,
                process_id=process_id,
                process_locked=False,
                title="抽检用例问诊",
                object_label="对象",
                topic_summary="主题",
                stage="prescribe",
                rounds=1,
                status=ConsultStatus.RESOLVED.value,
                triage_snapshot={},
                slots_snapshot={},
                user_id=_USER,
            )
        )
        card_id = new_id("card")
        ids.append(card_id)
        db.add(
            PrescriptionCard(
                id=card_id,
                consultation_id=consult_id,
                diagnosis={},
                actions=[],
                pesticide_items=[],
                facts_provenance={},
                gate_result={},
                gate_config_version=0,
                requires_tech_station=False,
                ledger_saved=False,
                created_at=_day_start(day) + timedelta(minutes=index + 1),
                process_id=process_id,
                user_id=_USER,
            )
        )
    await db.flush()
    return ids


# ---- 1. 按流程分层：七条流程各至少 3 例 ----


async def test_build_daily_batch_stratifies_by_process(session_factory) -> None:
    """七条流程各造 3 张卡：批次里每条流程恰好 3 例，无 `insufficient`。"""
    day = _today()
    async with session_factory() as db:
        for process in ProcessId:
            await _seed_cards(db, process.value, 3, day)
        await db.commit()

        batch = await QualitySampleService(db).build_daily_batch(day)
        await db.commit()

    assert batch.run_date == day
    assert batch.insufficient == ()
    assert len(batch.items) == 3 * len(ProcessId)
    by_process: dict[str, int] = {}
    for item in batch.items:
        by_process[item.process_id] = by_process.get(item.process_id, 0) + 1
    assert set(by_process) == {process.value for process in ProcessId}
    assert all(count == 3 for count in by_process.values())


async def test_batch_rows_carry_required_fields(session_factory) -> None:
    """入库行含 `prescription_card_id` / `process_id`，标注两列初始为 `None`。"""
    day = _today()
    async with session_factory() as db:
        await _seed_cards(db, "rice", 3, day)
        await db.commit()

        await QualitySampleService(db).build_daily_batch(day)
        await db.commit()

        rows = (await db.execute(select(EvalRun).where(EvalRun.kind == KIND_QUALITY_SAMPLE))).scalars().all()

    assert len(rows) == 3
    for row in rows:
        assert row.prescription_card_id
        assert row.process_id == "rice"
        assert row.quality_label is None
        assert row.quality_labeled_at is None
        assert row.triage_correct is None


# ---- 2. 幂等 ----


async def test_build_daily_batch_is_idempotent(session_factory) -> None:
    """同一天重复调用：第二次不再新增样本（已入样的卡被 `exclude_ids` 排除）。"""
    day = _today()
    async with session_factory() as db:
        await _seed_cards(db, "tea", 3, day)
        await db.commit()

        first = await QualitySampleService(db).build_daily_batch(day)
        await db.commit()
        second = await QualitySampleService(db).build_daily_batch(day)
        await db.commit()

        total = (await db.execute(select(EvalRun).where(EvalRun.kind == KIND_QUALITY_SAMPLE))).scalars().all()

    assert len(first.items) == 3
    assert second.items == ()
    assert len(total) == 3, "补跑不得产生重复行"


async def test_rerun_picks_up_only_new_vacancies(session_factory) -> None:
    """补跑只抽"尚未入样"的候选：当天候选从 2 张变 4 张时，第二次只抽新增的 2 张，不重复。"""
    day = _today()
    async with session_factory() as db:
        await _seed_cards(db, "rapeseed", 2, day)
        await db.commit()

        first = await QualitySampleService(db).build_daily_batch(day)
        await db.commit()
        assert len(first.items) == 2
        assert "rapeseed" in first.insufficient

        # 当天又新增两张（模拟白天持续开方）
        await _seed_cards(db, "rapeseed", 2, day)
        await db.commit()

        second = await QualitySampleService(db).build_daily_batch(day)
        await db.commit()
        assert len(second.items) == 2, "只抽尚未入样的候选"
        assert "rapeseed" not in second.insufficient, "累计已达标（4 >= 3）"

        all_cards = [
            row.prescription_card_id
            for row in (await db.execute(select(EvalRun).where(EvalRun.kind == KIND_QUALITY_SAMPLE))).scalars().all()
        ]

    assert len(all_cards) == len(set(all_cards)), "不得重复入样"


# ---- 3. 样本不足不补历史 ----


async def test_insufficient_marked_when_below_minimum(session_factory) -> None:
    """某条流程当日不足 3 例：返回实际条数并标 `insufficient`，不拿历史数据凑数。"""
    day = _today()
    yesterday = day - timedelta(days=1)
    async with session_factory() as db:
        await _seed_cards(db, "pig", 1, day)
        # 昨天的卡：不能用来补今天的缺口
        await _seed_cards(db, "pig", 5, yesterday)
        await db.commit()

        batch = await QualitySampleService(db).build_daily_batch(day)
        await db.commit()

    pig_items = [item for item in batch.items if item.process_id == "pig"]
    assert len(pig_items) == 1
    assert "pig" in batch.insufficient
    # 其余六条流程一张卡都没有，全部标 insufficient
    assert set(batch.insufficient) == {process.value for process in ProcessId}


# ---- 4. 回退触发 ----


def test_should_rollback_prompt_two_consecutive_days() -> None:
    """两日均超阈值 True；仅一日超阈值 False（单日越线可能是抽样波动）。"""
    threshold = get_settings().QUALITY_BAD_RATE_THRESHOLD
    assert should_rollback_prompt(threshold + 0.05, threshold + 0.05) is True
    assert should_rollback_prompt(threshold + 0.05, 0.0) is False
    assert should_rollback_prompt(0.0, threshold + 0.05) is False
    assert should_rollback_prompt(threshold, threshold) is False, "等于阈值不算越线"


async def _label(db, run_date: date, label: str, index: int) -> None:
    db.add(
        EvalRun(
            id=new_id("evr"),
            kind=KIND_QUALITY_SAMPLE,
            run_date=run_date,
            process_id="rice",
            prescription_card_id=new_id("card"),
            quality_label=label,
            quality_labeled_at=datetime.now(UTC),
            payload={},
        )
    )


async def test_rollback_ticket_contains_two_day_samples_and_versions(session_factory) -> None:
    """连续两日不可用占比越线：生成工单，含两日样本清单与新旧提示词版本号。"""
    from app.agent.prompts.registry import current_prompt_version
    from app.observability.sampling import _maybe_rollback_ticket

    today = _today()
    yesterday = today - timedelta(days=1)
    async with session_factory() as db:
        # 前两天各 1 条不可用 + 0 条可用 → 当日不可用占比 1.0 > 0.10
        for day in (today, yesterday):
            await _label(db, day, QUALITY_LABEL_UNUSABLE, 0)
        await db.commit()

        repo = MetricsRepository(db)
        created = await _maybe_rollback_ticket(db, repo, today)
        await db.commit()

        tickets = (await db.execute(select(EvalRun).where(EvalRun.kind == KIND_ROLLBACK_TICKET))).scalars().all()

    assert created is True
    assert len(tickets) == 1
    payload = tickets[0].payload
    assert payload["prompt_name"] == "prescriber"
    assert payload["current_version"] == current_prompt_version("prescriber")
    assert payload["previous_version"] == max(current_prompt_version("prescriber") - 1, 0)
    # 两日的样本清单都在工单里（键是 ISO 日期）
    assert today.isoformat() in payload["unusable_samples"]
    assert yesterday.isoformat() in payload["unusable_samples"]
    assert payload["unusable_samples"][today.isoformat()], "工单要能点开逐条复核"
    assert set(payload["bad_rates"]) == {today.isoformat(), yesterday.isoformat()}


async def test_no_rollback_ticket_when_only_one_day_over(session_factory) -> None:
    """仅一日越线：不生成工单。"""
    from app.observability.sampling import _maybe_rollback_ticket

    today = _today()
    async with session_factory() as db:
        await _label(db, today, QUALITY_LABEL_UNUSABLE, 0)
        # 昨天：全部可用，占比 0.0
        await _label(db, today - timedelta(days=1), QUALITY_LABEL_USABLE, 0)
        await db.commit()

        created = await _maybe_rollback_ticket(db, MetricsRepository(db), today)
        await db.commit()

        tickets = (await db.execute(select(EvalRun).where(EvalRun.kind == KIND_ROLLBACK_TICKET))).scalars().all()

    assert created is False
    assert tickets == []


async def test_quality_sample_bad_rate_counts_only_labeled(session_factory) -> None:
    """不可用占比的分母只算已标注行：未标注不构成不合格。"""
    today = _today()
    async with session_factory() as db:
        await _label(db, today, QUALITY_LABEL_UNUSABLE, 0)
        await _label(db, today, QUALITY_LABEL_USABLE, 1)
        await _label(db, today, QUALITY_LABEL_USABLE, 2)
        db.add(
            EvalRun(
                id=new_id("evr"),
                kind=KIND_QUALITY_SAMPLE,
                run_date=today,
                process_id="rice",
                prescription_card_id=new_id("card"),
                quality_label=None,
                payload={},
            )
        )
        await db.commit()

        metric = await MetricsRepository(db).quality_sample_bad_rate(today)

    assert metric.denominator == 3, "未标注行不进分母"
    assert metric.numerator == 1
    assert metric.rate == pytest.approx(1 / 3)


# ---- 5. 调度入口：一轮完成落库、抽检、快照 ----


async def test_collect_daily_metrics_runs_one_round(session_factory, monkeypatch) -> None:
    """`collect_daily_metrics` 一轮：构建抽检批次、写指标快照，并把统计返回给调用方。"""
    day = _today()

    class _SessionCtx:
        """把任务内的 `SessionLocal()` 换成测试会话工厂，其余流程走真实实现。"""

        def __init__(self, factory) -> None:
            self._factory = factory
            self._session = None

        def __call__(self):
            return self

        async def __aenter__(self):
            self._session = self._factory()
            return await self._session.__aenter__()

        async def __aexit__(self, *exc):
            return await self._session.__aexit__(*exc)

    # 先造好种子数据
    factory = session_factory
    async with factory() as db:
        for process in ProcessId:
            await _seed_cards(db, process.value, 3, day)
        await db.commit()

    import app.observability.sampling as sampling

    monkeypatch.setattr(sampling, "SessionLocal", lambda: _SessionCtx(factory))
    # 清掉可能残留的进程内样本，避免其它用例的直方图混进本轮
    from app.observability.metrics import metrics

    metrics.reset()

    stats = await collect_daily_metrics({})

    assert stats["quality_sample"] == 3 * len(ProcessId)
    assert stats["node_timing"] == 0
    assert stats["checkpoint_timing"] == 0
    assert stats["rollback_ticket"] == 0

    async with factory() as db:
        snapshots = (await db.execute(select(EvalRun).where(EvalRun.kind == "metrics_snapshot"))).scalars().all()

    assert len(snapshots) == 1
    assert "triage_accuracy" in snapshots[0].payload
