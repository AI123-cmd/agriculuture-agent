"""计量记录仓储与成本聚合（T2-59，技术方案 10.4）。

三个维度的聚合对应方案 10.4 的"按流程、按智能体、按任务的 token 成本明细"，全部经本仓
储走 `select()`，**不使用 `db.query(Model)`**（全局约定 L4）。聚合是成本看板（T2-59 的
`cost_dashboard_service`）与预算守卫（T2-31）的公共入口。

与 `KnowledgeReviewLogRepository` 同类：构造时绑定会话，方法不接受 `user_id`——计量是
全局观测数据，不承载归属语义（归属由行内的 `user_id` 字段表达）。

**成本单位**：聚合结果里的 `cost_micros` 仍是微元（1e-6 元）整数。浮点换算只在展示层
（服务层）做，仓储保持整数口径，避免聚合中途引入浮点误差。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_call import LlmCall


@dataclass(frozen=True, slots=True)
class AgentCostRow:
    """按智能体聚合的一行：调用次数、token、成本、失败次数。"""

    agent: str
    calls: int
    tokens: int
    cost_micros: int
    failures: int


@dataclass(frozen=True, slots=True)
class ProcessCostRow:
    """按流程聚合的一行。匿名或未标注流程的调用归到 `process_id=None` 一组。"""

    process_id: str | None
    calls: int
    tokens: int
    cost_micros: int


@dataclass(frozen=True, slots=True)
class TaskCostRow:
    """按任务类型聚合的一行。"""

    task_type: str
    calls: int
    tokens: int
    cost_micros: int


@dataclass(frozen=True, slots=True)
class DegradeRatioRow:
    """降级率与降档率。

    两者分开给：降档是预算控制的结果（正常），降级是服务故障的结果（异常），混在一起
    会掩盖问题。`total` 为 0 时两个比值都为 0.0（无调用即无降级）。
    """

    fallback_ratio: float
    degraded_ratio: float
    total: int
    fallback_count: int
    degraded_count: int


def cost_statement(dimension: Any, since: datetime, until: datetime) -> Select[Any]:
    """构造"按某维度聚合"的语句。维度是 `LlmCall` 上的一个列。

    抽成模块级函数是为了让"SQL 带 `created_at BETWEEN` 且按维度 `GROUP BY`"这条验收
    可以被直接断言（`str(cost_statement(...))`），不必依赖数据库。
    """
    tokens = func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens)
    return (
        select(
            dimension.label("dimension"),
            func.count().label("calls"),
            tokens.label("tokens"),
            func.sum(LlmCall.cost_micros).label("cost_micros"),
        )
        .where(LlmCall.created_at.between(since, until))
        .group_by(dimension)
        .order_by(func.sum(LlmCall.cost_micros).desc())
    )


def degrade_statement(since: datetime, until: datetime) -> Select[Any]:
    """构造"降级率与降档率"的语句。一条聚合返回总数与两个分类计数。"""
    return select(
        func.count().label("total"),
        func.sum(case((LlmCall.is_fallback.is_(True), 1), else_=0)).label("fallback"),
        func.sum(case((LlmCall.is_degraded.is_(True), 1), else_=0)).label("degraded"),
    ).where(LlmCall.created_at.between(since, until))


def _ratio(part: int, total: int) -> float:
    return part / total if total else 0.0


class LlmCallRepository:
    """计量记录仓储。所有成本聚合经此。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def cost_by_agent(self, since: datetime, until: datetime) -> list[AgentCostRow]:
        """按智能体聚合：调用次数、token、成本、失败次数，成本降序。"""
        failures = func.sum(case((LlmCall.ok.is_(False), 1), else_=0))
        by_agent = (
            select(
                LlmCall.agent.label("dimension"),
                func.count().label("calls"),
                func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens).label("tokens"),
                func.sum(LlmCall.cost_micros).label("cost_micros"),
                failures.label("failures"),
            )
            .where(LlmCall.created_at.between(since, until))
            .group_by(LlmCall.agent)
            .order_by(func.sum(LlmCall.cost_micros).desc())
        )
        rows = (await self._session.execute(by_agent)).all()
        return [
            AgentCostRow(
                agent=row.dimension,
                calls=row.calls,
                tokens=row.tokens or 0,
                cost_micros=row.cost_micros or 0,
                failures=row.failures or 0,
            )
            for row in rows
        ]

    async def cost_by_process(self, since: datetime, until: datetime) -> list[ProcessCostRow]:
        """按流程聚合，成本降序。"""
        rows = (await self._session.execute(cost_statement(LlmCall.process_id, since, until))).all()
        return [
            ProcessCostRow(
                process_id=row.dimension,
                calls=row.calls,
                tokens=row.tokens or 0,
                cost_micros=row.cost_micros or 0,
            )
            for row in rows
        ]

    async def cost_by_task_type(self, since: datetime, until: datetime) -> list[TaskCostRow]:
        """按任务类型聚合，成本降序。"""
        rows = (await self._session.execute(cost_statement(LlmCall.task_type, since, until))).all()
        return [
            TaskCostRow(
                task_type=row.dimension,
                calls=row.calls,
                tokens=row.tokens or 0,
                cost_micros=row.cost_micros or 0,
            )
            for row in rows
        ]

    async def degrade_ratio(self, since: datetime, until: datetime) -> DegradeRatioRow:
        """降级率（走备选）与降档率（用私有化候选），两个比值分别给出。"""
        row = (await self._session.execute(degrade_statement(since, until))).one()
        total = int(row.total or 0)
        fallback = int(row.fallback or 0)
        degraded = int(row.degraded or 0)
        return DegradeRatioRow(
            fallback_ratio=_ratio(fallback, total),
            degraded_ratio=_ratio(degraded, total),
            total=total,
            fallback_count=fallback,
            degraded_count=degraded,
        )

    async def sum_tokens_for_consultation(self, consultation_id: str) -> int:
        """单次问诊累计 token。预算守卫（T2-31）在每次调用前查它做降档判断。

        走 `ix_llm_call_consultation_id`；只取 token 之和，不取成本——预算是按 token 计的。
        """
        stmt = select(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens)).where(
            LlmCall.consultation_id == consultation_id
        )
        return int((await self._session.execute(stmt)).scalar_one_or_none() or 0)

    async def list_in_window(self, since: datetime, until: datetime) -> Sequence[LlmCall]:
        """窗口内的原始记录，按时间升序（导出与排查用）。"""
        stmt = select(LlmCall).where(LlmCall.created_at.between(since, until)).order_by(LlmCall.created_at, LlmCall.id)
        return list((await self._session.execute(stmt)).scalars().all())


__all__ = [
    "AgentCostRow",
    "DegradeRatioRow",
    "LlmCallRepository",
    "ProcessCostRow",
    "TaskCostRow",
    "cost_statement",
    "degrade_statement",
]
