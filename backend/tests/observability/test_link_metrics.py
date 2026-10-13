"""T6-09 验收（二）：四项链路级指标（P50/P95、失败率、token 成本、检查点写入耗时）。

断言要点：

1. **节点耗时 P50/P95** 从落库明细（`eval_run` 的 `node_timing`）用 `percentile_cont` 算，
   同一数据集必须满足 `P50 <= P95`；按 `node` 与 `process_id` 切分。
2. **失败率与降级** 按节点分组，`ok=false` 计失败、`degraded=true` 计降级，两者分开。
3. **token 消耗与成本** 委托 `LlmCallRepository`，按 `agent` 与 `task_type` 两个维度。
4. **检查点写入耗时**：`observe_checkpoint_write_ms` 采样后由落库写入器写进 `eval_run`，
   再从仓储查得——直方图跨进程汇总必须落库。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import new_id
from app.models import EvalRun, LlmCall
from app.models.eval_run import KIND_CHECKPOINT_TIMING, KIND_NODE_TIMING
from app.observability.metrics import (
    drain_checkpoint_write_samples,
    drain_node_duration_groups,
    metrics,
    observe_checkpoint_write_ms,
    observe_node_duration,
)
from app.repositories.metrics import MetricsRepository

pytestmark = pytest.mark.usefixtures("requires_postgres")

_USER = "user-t609-link"


@pytest.fixture(autouse=True)
def _reset_collector():
    """进程内采集器是全局单例：用例之间必须清空，否则样本相互污染。"""
    metrics.reset()
    yield
    metrics.reset()


def _today():
    return datetime.now(UTC).date()


def _window(days: int = 1) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(days=days), now + timedelta(days=1)


def _node_row(
    *,
    node: str,
    duration_ms: int,
    process_id: str | None = "rice",
    ok: bool = True,
    degraded: bool = False,
) -> EvalRun:
    return EvalRun(
        id=new_id("evr"),
        kind=KIND_NODE_TIMING,
        run_date=_today(),
        process_id=process_id,
        node=node,
        duration_ms=duration_ms,
        ok=ok,
        degraded=degraded,
        payload={},
    )


# ---- 1. 节点耗时 P50/P95 ----


async def test_node_latency_p50_le_p95(session_factory) -> None:
    """同一数据集上 P50 <= P95，且样本数等于落库行数。"""
    async with session_factory() as db:
        db.add_all([_node_row(node="diagnoser", duration_ms=value) for value in (100, 200, 300, 400, 5000)])
        await db.commit()

        since, until = _window()
        rows = await MetricsRepository(db).node_latency(since, until)

    assert len(rows) == 1
    row = rows[0]
    assert row.node == "diagnoser"
    assert row.quantile.samples == 5
    assert row.quantile.p50 <= row.quantile.p95
    # 5000 是长尾：P95 应明显高于中位数
    assert row.quantile.p95 > row.quantile.p50


async def test_node_latency_can_split_by_process(session_factory) -> None:
    """按流程切分：指定 `process_id` 时只统计该流程的节点。"""
    async with session_factory() as db:
        db.add_all(
            [
                _node_row(node="prescriber", duration_ms=100, process_id="rice"),
                _node_row(node="prescriber", duration_ms=900, process_id="tea"),
            ]
        )
        await db.commit()

        since, until = _window()
        rows = await MetricsRepository(db).node_latency(since, until, process_id="rice")

    assert len(rows) == 1
    assert rows[0].process_id == "rice"
    assert rows[0].quantile.samples == 1
    assert rows[0].quantile.p50 == pytest.approx(100.0)


async def test_node_latency_empty_dataset_is_zero(session_factory) -> None:
    """无样本：返回空列表（不构造 0 值行，避免"没数据"看起来像"耗时为 0"）。"""
    async with session_factory() as db:
        since, until = _window()
        rows = await MetricsRepository(db).node_latency(since, until)

    assert rows == []


# ---- 2. 失败率与降级 ----


async def test_node_failure_counts_failures_and_degraded_separately(session_factory) -> None:
    """失败率 = `ok=false` 占比；降级单独计数，不与失败混在一起。"""
    async with session_factory() as db:
        db.add_all(
            [
                _node_row(node="gate", duration_ms=10, ok=True, degraded=False),
                _node_row(node="gate", duration_ms=12, ok=True, degraded=True),
                _node_row(node="gate", duration_ms=15, ok=False, degraded=False),
                _node_row(node="gate", duration_ms=11, ok=False, degraded=True),
            ]
        )
        await db.commit()

        since, until = _window()
        rows = await MetricsRepository(db).node_failure(since, until)

    assert len(rows) == 1
    row = rows[0]
    assert row.node == "gate"
    assert row.total == 4
    assert row.failures == 2
    assert row.degraded == 2
    assert row.failure_rate == pytest.approx(0.5)


async def test_node_failure_empty_is_zero(session_factory) -> None:
    async with session_factory() as db:
        since, until = _window()
        rows = await MetricsRepository(db).node_failure(since, until)

    assert rows == []


# ---- 3. token 消耗与成本 ----


def _llm_call(
    *, agent: str, task_type: str, prompt: int, completion: int, cost_micros: int, ok: bool = True
) -> LlmCall:
    return LlmCall(
        user_id=_USER,
        process_id="rice",
        agent=agent,
        task_type=task_type,
        tier="standard",
        provider="farm",
        model="farm-llm-v1",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_micros=cost_micros,
        latency_ms=120,
        is_fallback=False,
        is_degraded=False,
        ok=ok,
    )


async def test_token_and_cost_by_agent(session_factory) -> None:
    """按智能体聚合 token 与成本，成本降序。"""
    async with session_factory() as db:
        db.add_all(
            [
                _llm_call(agent="diagnoser", task_type="diagnosis", prompt=100, completion=50, cost_micros=1000),
                _llm_call(agent="diagnoser", task_type="diagnosis", prompt=200, completion=60, cost_micros=2000),
                _llm_call(agent="prescriber", task_type="prescription", prompt=300, completion=80, cost_micros=9000),
            ]
        )
        await db.commit()

        since, until = _window()
        rows = await MetricsRepository(db).token_and_cost_by_agent(since, until)

    assert [row.agent for row in rows] == ["prescriber", "diagnoser"]
    top = rows[0]
    assert top.calls == 1
    assert top.tokens == 380
    assert top.cost_micros == 9000
    diag = rows[1]
    assert diag.calls == 2
    assert diag.tokens == 410
    assert diag.cost_micros == 3000


async def test_token_and_cost_by_task_type(session_factory) -> None:
    """按任务类型聚合 token 与成本。"""
    async with session_factory() as db:
        db.add_all(
            [
                _llm_call(agent="diagnoser", task_type="diagnosis", prompt=100, completion=50, cost_micros=1000),
                _llm_call(agent="prescriber", task_type="prescription", prompt=300, completion=80, cost_micros=9000),
            ]
        )
        await db.commit()

        since, until = _window()
        rows = await MetricsRepository(db).token_and_cost_by_task_type(since, until)

    assert {row.task_type for row in rows} == {"diagnosis", "prescription"}
    assert rows[0].task_type == "prescription"  # 成本降序


# ---- 4. 检查点写入耗时 ----


async def test_checkpoint_write_latency_from_library(session_factory) -> None:
    """`observe_checkpoint_write_ms` 采样 → 落库 → 可从仓储查到 P50/P95。"""
    observe_checkpoint_write_ms(12.0)
    observe_checkpoint_write_ms(30.0)
    observe_checkpoint_write_ms(48.0)
    samples = drain_checkpoint_write_samples()
    assert samples == [12.0, 30.0, 48.0]

    async with session_factory() as db:
        repo = MetricsRepository(db)
        written = await repo.record_checkpoint_timing(_today(), samples=samples)
        await db.commit()

        since, until = _window()
        quantile = await repo.checkpoint_write_latency(since, until)

    assert written == 3
    assert quantile.samples == 3
    assert quantile.p50 <= quantile.p95
    assert quantile.p50 == pytest.approx(30.0)


async def test_drain_checkpoint_samples_empties_histogram() -> None:
    """`drain` 读完即清空：否则下一轮会把同一批样本再写一次，P50/P95 随轮数膨胀。"""
    observe_checkpoint_write_ms(1.0)
    assert drain_checkpoint_write_samples() == [1.0]
    assert drain_checkpoint_write_samples() == []


async def test_node_duration_groups_carry_node_and_process_tags() -> None:
    """节点耗时采样带 `node` / `process_id` 标签，落库时才能按两维度切分。"""
    observe_node_duration("diagnoser", 42.0, process_id="rice", ok=True)
    observe_node_duration("diagnoser", 55.0, process_id="rice", ok=False)
    groups = drain_node_duration_groups()

    assert len(groups) == 1
    tags, values = groups[0]
    assert tags == {"node": "diagnoser", "process_id": "rice"}
    assert values == [42.0, 55.0]
    # 读完即清空
    assert drain_node_duration_groups() == []


async def test_record_node_timing_spreads_failures_to_rows(session_factory) -> None:
    """失败/降级计数摊到行上：`ok`/`degraded` 列与行一一对应，失败率才可由 SQL 还原。"""
    async with session_factory() as db:
        repo = MetricsRepository(db)
        written = await repo.record_node_timing(
            _today(),
            "diagnoser",
            samples=[10.0, 20.0, 30.0],
            process_id="rice",
            failures=1,
            degraded=2,
        )
        await db.commit()

        since, until = _window()
        rows = await repo.node_failure(since, until)

    assert written == 3
    assert len(rows) == 1
    assert rows[0].total == 3
    assert rows[0].failures == 1
    assert rows[0].degraded == 2


async def test_record_node_timing_skips_empty_samples(session_factory) -> None:
    """空样本不写行：空导出不产生无意义记录。"""
    async with session_factory() as db:
        written = await MetricsRepository(db).record_node_timing(_today(), "diagnoser", samples=[])

    assert written == 0


async def test_metrics_snapshot_row_is_persisted(session_factory) -> None:
    """指标快照落库：`kind=metrics_snapshot` 的行带 payload，供跨天对比。"""
    from sqlalchemy import select

    async with session_factory() as db:
        await MetricsRepository(db).insert_metrics_snapshot(_today(), {"handoff_rate": {"value": 0.1}})
        await db.commit()

        row = (await db.execute(select(EvalRun).where(EvalRun.kind == "metrics_snapshot"))).scalar_one()

    assert row.payload["handoff_rate"]["value"] == pytest.approx(0.1)
