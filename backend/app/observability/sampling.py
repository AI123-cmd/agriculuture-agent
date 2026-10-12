"""指标落库与抽检的调度入口（T6-09，技术方案 14.3 / 14.5）。

本模块是**调用方**而非计算方：口径都在 `app/repositories/metrics.py`（聚合查询与落库写入器）
与 `app/observability/metrics.py`（进程内采集与纯函数）里，这里只负责"什么时候把哪批数据
搬到哪里"，供 ARQ 的定时任务调用。

三件事，一轮完成：

1. **导出节点耗时明细**：`traced_node` 每轮把耗时写进进程内直方图（带 `node` / `process_id`
   标签），本任务把它落进 `eval_run`（`kind=node_timing`）。直方图在进程重启后丢失，而
   "某次发版后 P95 劣化"需要跨天对比，故必须落库。失败与降级计数从同标签的计数器取回，
   摊到对应行上。
2. **导出检查点写入耗时**：`observe_checkpoint_write_ms` 的样本同样落库（`kind=checkpoint_timing`）。
3. **构建当日抽检批次**（`QualitySampleService`），并按"连续两日不可用占比越线"判断是否生成
   提示词回退工单。

**一步一清空**：直方图与计数器在导出后即 `drain` / `take`（读完删），否则下一轮会把同一批
样本再写一次，P50/P95 随轮数膨胀。代价是"导出与提交之间进程崩溃"会丢这一批样本——对观测
数据可接受，换来的是不重复计数，而重复计数会让指标系统性偏移。

**为什么 `process_id` 从标签取**：`observe_node_duration` 把 `process_id` 写进直方图标签，
落库时按同一标签分组还原，使 P50/P95 与失败率可按流程切分（14.3）。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.observability import log
from app.observability.metrics import (
    NODE_DEGRADED_METRIC,
    NODE_FAILURE_METRIC,
    drain_checkpoint_write_samples,
    drain_node_duration_groups,
    metrics,
    should_rollback_prompt,
)
from app.repositories.metrics import MetricsRepository, default_window
from app.services.quality_sample_service import QualitySampleService

# 回退工单关联的提示词模板名。抽检针对"模型改写质量"，对应的环节是处方改写（`prescriber`），
# 故回退提示词也指向它；换个模板名会让工单指向一个与抽检无关的环节。
_ROLLBACK_PROMPT_NAME = "prescriber"


def _triggered(day_rates: list[float]) -> bool:
    """连续 N 日不可用占比全部越线即触发回退。

    `day_rates` 按**时间升序**给出（越靠后越新）。两日窗口直接委托纯函数
    `should_rollback_prompt(today, yesterday)`，保证"连续两日"这一默认口径只有一处定义；
    配置把连续天数调大（>2）时退化为"窗口内每日都越线"的等价判断。
    """
    if len(day_rates) < 2:
        return False
    if len(day_rates) == 2:
        return should_rollback_prompt(day_rates[1], day_rates[0])
    threshold = float(get_settings().QUALITY_BAD_RATE_THRESHOLD)
    return all(rate > threshold for rate in day_rates)


async def _product_snapshot_payload(repo: MetricsRepository, run_date: datetime) -> dict[str, Any]:
    """当日六项产品级指标的快照值，写进 `eval_run`（`kind=metrics_snapshot`）。

    快照是看板的持久化底稿：跨天对比不依赖"进程内采集器还在不在"，与节点耗时落库同一动机。
    """
    since, until = default_window(now=run_date)
    triage = await repo.triage_accuracy(since, until)
    gate = await repo.gate_block_rate_excluding_shadow(since, until)
    subscribe = await repo.subscribe_once_delivery_rate(since, until)
    click = repo.snapshot_click_rate()
    rounds = await repo.rounds_distribution(since, until)
    handoff = await repo.handoff_rate(since, until)
    return {
        "triage_accuracy": {"value": triage.rate, "labeled": triage.denominator, "correct": triage.numerator},
        "gate_block_rate": {"value": gate.rate, "total": gate.denominator, "blocked": gate.numerator},
        "subscribe_once_delivery_rate": {"value": subscribe.rate, "attempted": subscribe.denominator},
        "snapshot_click_rate": {"available": click.available, "value": click.rate, "note": click.note},
        "rounds_distribution": {
            "counts": rounds.counts,
            "over_round": rounds.over_round,
            "done": rounds.done,
            "over_round_rate": rounds.over_round_rate,
        },
        "handoff_rate": {"value": handoff.rate, "total": handoff.denominator, "escalated": handoff.numerator},
    }


async def _maybe_rollback_ticket(db: AsyncSession, repo: MetricsRepository, run_date: Any) -> bool:
    """按连续 N 日不可用占比判断是否生成提示词回退工单，返回是否生成。

    工单不是自动回退：提示词回退会改变线上行为，必须有人确认（15.5 灰度原则）。工单携带两样
    东西供人决策——两个连续日期的 `quality_label=unusable` 样本清单（可点开逐条复核）、
    当前与上一提示词版本号（回退到哪一版）。
    """
    from app.agent.prompts.registry import current_prompt_version

    consecutive = max(int(get_settings().QUALITY_ROLLBACK_CONSECUTIVE_DAYS), 2)
    days = [run_date - timedelta(days=offset) for offset in range(consecutive - 1, -1, -1)]
    rates = [(day, (await repo.quality_sample_bad_rate(day)).rate) for day in days]
    if not _triggered([rate for _, rate in rates]):
        return False

    samples: dict[str, list[str]] = {}
    for day, _ in rates:
        rows = await repo.unusable_samples(day)
        samples[day.isoformat()] = [str(row.prescription_card_id) for row in rows]

    current = int(current_prompt_version(_ROLLBACK_PROMPT_NAME))
    payload: dict[str, Any] = {
        "prompt_name": _ROLLBACK_PROMPT_NAME,
        "current_version": current,
        "previous_version": max(current - 1, 0),
        "unusable_samples": samples,
        "bad_rates": {day.isoformat(): rate for day, rate in rates},
    }
    await repo.insert_rollback_ticket(run_date, payload)
    log.warning("生成提示词回退工单", prompt_name=_ROLLBACK_PROMPT_NAME, run_date=str(run_date))
    return True


async def collect_daily_metrics(ctx: dict[str, Any]) -> dict[str, int]:
    """cron 任务：把进程内指标落库、构建当日抽检批次、按需生成回退工单。

    会话在此自建并显式提交：ARQ 任务不在请求上下文里，事务边界由本函数负责。直方图在导出时
    即清空（`drain_*`），故提交失败会丢当轮样本——见模块说明的取舍。
    """
    run_date = datetime.now(UTC).date()
    async with SessionLocal() as db:
        repo = MetricsRepository(db)

        node_rows = 0
        for tags, samples in drain_node_duration_groups():
            node_rows += await repo.record_node_timing(
                run_date,
                tags.get("node") or "unknown",
                samples=samples,
                process_id=tags.get("process_id"),
                failures=metrics.take_counter(NODE_FAILURE_METRIC, tags=tags),
                degraded=metrics.take_counter(NODE_DEGRADED_METRIC, tags=tags),
            )

        checkpoint_rows = await repo.record_checkpoint_timing(run_date, samples=drain_checkpoint_write_samples())

        batch = await QualitySampleService(db).build_daily_batch(run_date)

        await repo.insert_metrics_snapshot(run_date, await _product_snapshot_payload(repo, datetime.now(UTC)))
        rollback = await _maybe_rollback_ticket(db, repo, run_date)

        await db.commit()

    stats = {
        "node_timing": node_rows,
        "checkpoint_timing": checkpoint_rows,
        "quality_sample": len(batch.items),
        "rollback_ticket": int(rollback),
    }
    if any(stats.values()):
        log.info("指标落库与抽检完成", **stats)
    return stats


__all__ = ["collect_daily_metrics"]
