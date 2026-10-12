"""线上指标只读接口（T6-09，技术方案 14.3）。

供内部看板按天拉取六项产品级指标，与农户侧接口分离。鉴权复用知识核准那套：`require_review_operator`
先验令牌（401）再查运营白名单（403），农户令牌无法调用——指标暴露的是全平台经营口径，
不是任何单个农户能看的数据。

**只读且无副作用**：本接口只聚合查询，不写任何表，也不触发采样（采样由 ARQ 的
`collect_daily_metrics` 定时跑）。看板刷新不该改变任何状态。

**统计窗口**：默认最近 1 个自然日（含今天），与调度入口 `default_window` 同口径；调用方可传
`days` 拉多日走势。窗口口径只在一处定义（`app/repositories/metrics.py` 的 `default_window`），
路由不自行拼时间范围。
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_review_operator
from app.core.response import ok
from app.db.session import get_db
from app.repositories.metrics import MetricsRepository, default_window

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/summary")
async def metrics_summary(
    days: int = Query(default=1, ge=1, le=30, description="统计窗口天数（含今天）"),
    db: AsyncSession = Depends(get_db),
    operator_id: str = Depends(require_review_operator),
) -> dict:
    """六项产品级指标的当日口径（口径固定在仓储方法内，路由不重算）。"""
    # 起点口径统一走 `default_window`（当日零点），多日窗口在其上回推 `days - 1` 天，
    # 不自行拼时间范围，避免出现第二套"窗口起点"算法。
    since, until = default_window(now=datetime.now(UTC))
    if days > 1:
        since = since - timedelta(days=days - 1)

    repo = MetricsRepository(db)
    triage = await repo.triage_accuracy(since, until)
    gate = await repo.gate_block_rate_excluding_shadow(since, until)
    subscribe = await repo.subscribe_once_delivery_rate(since, until)
    click = repo.snapshot_click_rate()
    rounds = await repo.rounds_distribution(since, until)
    handoff = await repo.handoff_rate(since, until)
    return ok(
        {
            "window": {"since": since.isoformat(), "until": until.isoformat(), "days": days},
            "triage_accuracy": {"rate": triage.rate, "labeled": triage.denominator, "correct": triage.numerator},
            "gate_block_rate": {"rate": gate.rate, "total": gate.denominator, "blocked": gate.numerator},
            "subscribe_once_delivery_rate": {
                "rate": subscribe.rate,
                "attempted": subscribe.denominator,
                "delivered": subscribe.numerator,
            },
            "snapshot_click_rate": {"available": click.available, "rate": click.rate, "note": click.note},
            "rounds_distribution": {
                "counts": rounds.counts,
                "over_round": rounds.over_round,
                "done": rounds.done,
                "over_round_rate": rounds.over_round_rate,
            },
            "handoff_rate": {"rate": handoff.rate, "total": handoff.denominator, "escalated": handoff.numerator},
        }
    )


__all__ = ["router"]
