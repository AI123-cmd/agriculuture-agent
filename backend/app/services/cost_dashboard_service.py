"""成本看板聚合入口（T2-59，技术方案 10.4）。

方案 10.4 要求成本控制从"原则"变成"可观测的数据"：本服务把 `llm_call` 表的三个维度聚合
（按流程、按智能体、按任务）与降级/降档率收成一个快照对象，供看板接口与预算守护消费。

**只做聚合与换算，不改数据**：看板是只读视图，成本归属的原始记录由 `TokenMeterImpl` 在
调用后写入。这里不做任何写操作，也不持有熔断/路由状态——它是一个纯粹的查询门面。

**微元到元的换算在此收口**：仓储层保持整数微元口径（避免聚合中途的浮点误差），展示层
统一在本服务里除以 `1e6` 转成元。看板接口与预算逻辑读到的"元"只有这一个来源。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.llm_call import (
    AgentCostRow,
    DegradeRatioRow,
    LlmCallRepository,
    ProcessCostRow,
    TaskCostRow,
)

_MICROS_PER_YUAN = 1_000_000


def micros_to_yuan(cost_micros: int) -> float:
    """微元转元。展示层唯一换算点。"""
    return cost_micros / _MICROS_PER_YUAN


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    """一次看板查询的完整结果。

    三个维度的行都以微元成本给出，另附换算后的元口径字段（`*_yuan` 由本模块的换算生成），
    让调用方不必各自实现一次除法。
    """

    since: datetime
    until: datetime
    by_agent: list[AgentCostRow]
    by_process: list[ProcessCostRow]
    by_task_type: list[TaskCostRow]
    degrade: DegradeRatioRow

    @property
    def total_cost_micros(self) -> int:
        return sum(row.cost_micros for row in self.by_task_type)

    @property
    def total_cost_yuan(self) -> float:
        return micros_to_yuan(self.total_cost_micros)


class CostDashboardService:
    """成本看板聚合服务。只读。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = LlmCallRepository(session)

    async def snapshot(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        days: int = 7,
        now: datetime | None = None,
    ) -> CostSnapshot:
        """取一个时间窗内的完整成本快照。

        不传 `since`/`until` 时按"最近 `days` 天"算：看板默认看近一周，这是成本波动最
        容易被人察觉的粒度。`now` 可注入，便于测试固定时间窗。
        """
        end = until or (now or datetime.now(UTC))
        start = since or (end - timedelta(days=days))
        return CostSnapshot(
            since=start,
            until=end,
            by_agent=await self._repo.cost_by_agent(start, end),
            by_process=await self._repo.cost_by_process(start, end),
            by_task_type=await self._repo.cost_by_task_type(start, end),
            degrade=await self._repo.degrade_ratio(start, end),
        )

    async def tokens_for_consultation(self, consultation_id: str) -> int:
        """单次问诊的累计 token，供预算守卫（T2-31）在调用前判断是否需要降档。"""
        return await self._repo.sum_tokens_for_consultation(consultation_id)


__all__ = ["CostDashboardService", "CostSnapshot", "micros_to_yuan"]
