"""线上指标聚合仓储（T6-09，技术方案 14.3）。

六项产品级指标与四项链路级指标的唯一查询出口，全部走 `select()`（全局约定 L4 禁止
`db.query`）。构造时绑定会话、方法不接受 `user_id`——指标是**全局口径**（跨农户），
不承载归属语义，与 `LlmCallRepository` / `KnowledgeReviewLogRepository` 同类。

三处按"代码现状"重新固定的口径（文档给的字段名在代码里不存在，故废弃无效引用）：

1. **闸门拦截率排除影子行**：`gate_audit` 没有 `passed` 列，落的是 `decision`
   （allow/degrade/block）。"未通过"= `decision != 'allow'`，并固定排除 `shadowed=True`
   的行——影子期只记录不拦截，计入会让灰度期的拦截率虚高。故方法名为
   `gate_block_rate_excluding_shadow`，口径写死在方法内，不留给调用方。
2. **一次性订阅达成率**：`reach_record` 没有 `wake_up` 列，落的是 `channel` + `status`。
   达成率 = `channel='subscribe_once'` 且 `status='delivered'` 的记录数 / 全部
   `subscribe_once` 记录数。
3. **快照点击率**：`prompt_snapshot` 没有点击/展示列，`register_click` 只写 Redis 不落库，
   业务库无点击数据源。故本指标显式返回"不可得"（`available=False`）并给出原因，**不**
   用别的字段（如 `prompt_item.executed` 执行勾选）冒充——执行率是另一个口径（北极星指标），
   混用会让两个指标互相污染。

P50/P95 用 SQL 的 `percentile_cont` 从 `eval_run` 的落库明细算，不用进程内直方图：直方图在
进程重启后丢失，而"某次发版后 P95 劣化"通常需要跨天对比（14.3）。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import Consultation, EvalRun, GateAudit, ReachRecord
from app.models.enums import Channel, ConsultStatus, ReachStatus
from app.models.eval_run import (
    KIND_CHECKPOINT_TIMING,
    KIND_METRICS_SNAPSHOT,
    KIND_NODE_TIMING,
    KIND_QUALITY_SAMPLE,
    KIND_ROLLBACK_TICKET,
    QUALITY_LABEL_UNUSABLE,
)
from app.observability.metrics import DONE_STATUSES
from app.observability.metrics import over_round_rate as compute_over_round_rate
from app.repositories.llm_call import AgentCostRow, LlmCallRepository, TaskCostRow

# 一次性订阅渠道与送达状态。
_SUBSCRIBE_ONCE = Channel.SUBSCRIBE_ONCE.value
_DELIVERED = ReachStatus.DELIVERED.value


@dataclass(frozen=True, slots=True)
class RateMetric:
    """一个"分子 / 分母 → 比值"的通用形状。分母为 0 时比值为 0.0（无样本不构成失败）。"""

    numerator: int
    denominator: int
    rate: float

    @classmethod
    def of(cls, numerator: int, denominator: int) -> "RateMetric":
        return cls(numerator, denominator, numerator / denominator if denominator else 0.0)


@dataclass(frozen=True, slots=True)
class SnapshotClickMetric:
    """快照点击率。业务库无点击数据源时 `available=False`（`rate` 为 `None`）。"""

    available: bool
    rate: float | None
    note: str


@dataclass(frozen=True, slots=True)
class RoundsDistribution:
    """追问轮次分布（`consultation.rounds` 直方图）与"超轮次占比"。

    `over_round` 与 `done` 一并给出：单看比值无法判断是"少量长问诊"还是"普遍超轮次"。
    """

    counts: dict[int, int] = field(default_factory=dict)
    over_round: int = 0
    done: int = 0
    over_round_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class Quantile:
    """一组耗时样本的分位数。`samples` 为样本数，样本为 0 时两个分位数均为 0.0。"""

    p50: float
    p95: float
    samples: int


@dataclass(frozen=True, slots=True)
class NodeLatencyRow:
    """按节点（可选按流程）切分的耗时分位数。"""

    node: str
    process_id: str | None
    quantile: Quantile


@dataclass(frozen=True, slots=True)
class NodeFailureRow:
    """按节点切分的失败率与降级（重试）次数。"""

    node: str
    total: int
    failures: int
    degraded: int

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0


class MetricsRepository:
    """线上指标聚合。构造时绑定会话，方法自行执行。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._llm = LlmCallRepository(session)

    # ---- 六项产品级指标 ----

    async def triage_accuracy(self, since: datetime, until: datetime) -> RateMetric:
        """分流准确率：人工抽检判定的 `process_id` 正确数 / 已标注数。

        代码库无处存放"分诊正确答案"（分诊是模型行为，没有真值列），故由抽检标注补齐：
        `eval_run` 的 `quality_sample` 行上带 `triage_correct`。未标注的行不进分母，
        否则"还没标注"会被误算成"分错了"。
        """
        stmt = select(EvalRun.triage_correct).where(
            EvalRun.kind == KIND_QUALITY_SAMPLE,
            EvalRun.run_date >= since.date(),
            EvalRun.run_date <= until.date(),
            EvalRun.triage_correct.is_not(None),
        )
        labeled = list((await self._session.execute(stmt)).scalars().all())
        correct = sum(1 for value in labeled if value)
        return RateMetric.of(correct, len(labeled))

    async def gate_block_rate_excluding_shadow(self, since: datetime, until: datetime) -> RateMetric:
        """闸门拦截率：未通过（`decision != 'allow'`）占比，**固定排除影子行**。

        方法名即口径：调用方无法把它用成"含影子行"的版本。影子期（`shadowed=True`）只记录
        不拦截，计入会低估强制期的真实比例、误导"生成侧质量差"的判断（技术方案 14.2/14.3）。
        """
        total_stmt = (
            select(func.count())
            .select_from(GateAudit)
            .where(
                GateAudit.created_at.between(since, until),
                GateAudit.shadowed.is_(False),
            )
        )
        blocked_stmt = (
            select(func.count())
            .select_from(GateAudit)
            .where(
                GateAudit.created_at.between(since, until),
                GateAudit.shadowed.is_(False),
                GateAudit.decision != "allow",
            )
        )
        total = int((await self._session.execute(total_stmt)).scalar_one())
        blocked = int((await self._session.execute(blocked_stmt)).scalar_one())
        return RateMetric.of(blocked, total)

    async def subscribe_once_delivery_rate(self, since: datetime, until: datetime) -> RateMetric:
        """一次性订阅达成率：`subscribe_once` 渠道中已送达数 / 尝试数。

        分母是"尝试过一次性订阅触达"的记录数（含失败），分子是其中真正送达的——达成率低
        说明授权或时段策略有问题（技术方案 14.3"主动触达是否有效"）。
        """
        total_stmt = (
            select(func.count())
            .select_from(ReachRecord)
            .where(ReachRecord.channel == _SUBSCRIBE_ONCE, ReachRecord.attempted_at.between(since, until))
        )
        delivered_stmt = (
            select(func.count())
            .select_from(ReachRecord)
            .where(
                ReachRecord.channel == _SUBSCRIBE_ONCE,
                ReachRecord.status == _DELIVERED,
                ReachRecord.attempted_at.between(since, until),
            )
        )
        attempted = int((await self._session.execute(total_stmt)).scalar_one())
        delivered = int((await self._session.execute(delivered_stmt)).scalar_one())
        return RateMetric.of(delivered, attempted)

    def snapshot_click_rate(self) -> SnapshotClickMetric:
        """快照点击率。业务库无数据源，显式返回不可得（见模块说明第 3 条）。"""
        return SnapshotClickMetric(
            available=False,
            rate=None,
            note="业务库无快照点击落库列（register_click 仅写 Redis），本指标不可得；不以后续字段替代",
        )

    async def rounds_distribution(self, since: datetime, until: datetime) -> RoundsDistribution:
        """追问轮次分布与"超轮次占比"。

        `over_round_rate` 的计算委托纯函数（`app/observability/metrics.py`）：阈值取
        `Settings.MAX_COLLECT_ROUNDS`、分母排除 `observing` 中间态的口径都收在那一个函数里，
        仓储只负责把行取回来，不复制一份判定逻辑。
        """
        stmt = select(Consultation.status, Consultation.rounds).where(Consultation.create_time.between(since, until))
        rows = (await self._session.execute(stmt)).all()
        counts: dict[int, int] = {}
        for _status, rounds in rows:
            counts[int(rounds)] = counts.get(int(rounds), 0) + 1
        proxy = [_RoundsRow(status=str(status), rounds=int(rounds)) for status, rounds in rows]
        done = [row for row in proxy if row.status in DONE_STATUSES]
        over = sum(1 for row in done if row.rounds > _max_rounds())
        return RoundsDistribution(
            counts=dict(sorted(counts.items())),
            over_round=over,
            done=len(done),
            over_round_rate=compute_over_round_rate(proxy),
        )

    async def handoff_rate(self, since: datetime, until: datetime) -> RateMetric:
        """转人工率：`escalated` 问诊数 / **问诊总数**。

        分母是问诊总数而非已开方数（14.3 第二条口径）：转人工的案例恰恰没有处方，若按已开方
        计算，这批案例全被排除，指标恒偏低而失去意义。
        """
        total_stmt = (
            select(func.count()).select_from(Consultation).where(Consultation.create_time.between(since, until))
        )
        escalated_stmt = (
            select(func.count())
            .select_from(Consultation)
            .where(
                Consultation.create_time.between(since, until),
                Consultation.status == ConsultStatus.ESCALATED.value,
            )
        )
        total = int((await self._session.execute(total_stmt)).scalar_one())
        escalated = int((await self._session.execute(escalated_stmt)).scalar_one())
        return RateMetric.of(escalated, total)

    # ---- 四项链路级指标 ----

    async def node_latency(
        self,
        since: datetime,
        until: datetime,
        *,
        process_id: str | None = None,
    ) -> list[NodeLatencyRow]:
        """各节点耗时 P50/P95（可按流程切分），用 SQL 的 `percentile_cont` 从落库明细算。

        取 `duration_ms` 非空的 `node_timing` 行；`percentile_cont` 是连续插值分位数，
        在小样本上比 `percentile_disc` 更稳定。
        """
        conditions = [
            EvalRun.kind == KIND_NODE_TIMING,
            EvalRun.run_date >= since.date(),
            EvalRun.run_date <= until.date(),
            EvalRun.duration_ms.is_not(None),
        ]
        if process_id is not None:
            conditions.append(EvalRun.process_id == process_id)
        p50 = func.percentile_cont(0.5).within_group(EvalRun.duration_ms.asc())
        p95 = func.percentile_cont(0.95).within_group(EvalRun.duration_ms.asc())
        stmt = (
            select(EvalRun.node, EvalRun.process_id, p50.label("p50"), p95.label("p95"), func.count().label("samples"))
            .where(*conditions)
            .group_by(EvalRun.node, EvalRun.process_id)
            .order_by(func.count().desc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            NodeLatencyRow(
                node=str(row.node),
                process_id=row.process_id,
                quantile=Quantile(p50=float(row.p50 or 0.0), p95=float(row.p95 or 0.0), samples=int(row.samples)),
            )
            for row in rows
        ]

    async def node_failure(self, since: datetime, until: datetime) -> list[NodeFailureRow]:
        """各节点失败率与降级（重试）次数，按节点分组。

        失败 = `ok=false`；降级 = `degraded=true`（正常返回但走了备选/降档）。两者分开计数：
        正常返回的降级最容易被忽略，与失败混在一起就看不出。
        """
        failures = func.sum(case((EvalRun.ok.is_(False), 1), else_=0))
        degraded = func.sum(case((EvalRun.degraded.is_(True), 1), else_=0))
        stmt = (
            select(
                EvalRun.node,
                func.count().label("total"),
                failures.label("failures"),
                degraded.label("degraded"),
            )
            .where(
                EvalRun.kind == KIND_NODE_TIMING,
                EvalRun.run_date >= since.date(),
                EvalRun.run_date <= until.date(),
            )
            .group_by(EvalRun.node)
            .order_by(func.count().desc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            NodeFailureRow(
                node=str(row.node),
                total=int(row.total),
                failures=int(row.failures or 0),
                degraded=int(row.degraded or 0),
            )
            for row in rows
        ]

    async def token_and_cost_by_agent(self, since: datetime, until: datetime) -> list[AgentCostRow]:
        """各节点（智能体）token 消耗与成本，委托 `LlmCallRepository`（同一口径，不重写 SQL）。"""
        return await self._llm.cost_by_agent(since, until)

    async def token_and_cost_by_task_type(self, since: datetime, until: datetime) -> list[TaskCostRow]:
        """各任务类型 token 消耗与成本，委托 `LlmCallRepository`。"""
        return await self._llm.cost_by_task_type(since, until)

    async def checkpoint_write_latency(self, since: datetime, until: datetime) -> Quantile:
        """检查点写入耗时 P50/P95，从 `eval_run` 的 `checkpoint_timing` 明细算。"""
        conditions = [
            EvalRun.kind == KIND_CHECKPOINT_TIMING,
            EvalRun.run_date >= since.date(),
            EvalRun.run_date <= until.date(),
            EvalRun.duration_ms.is_not(None),
        ]
        p50 = func.percentile_cont(0.5).within_group(EvalRun.duration_ms.asc())
        p95 = func.percentile_cont(0.95).within_group(EvalRun.duration_ms.asc())
        stmt = select(p50.label("p50"), p95.label("p95"), func.count().label("samples")).where(*conditions)
        row = (await self._session.execute(stmt)).one()
        return Quantile(p50=float(row.p50 or 0.0), p95=float(row.p95 or 0.0), samples=int(row.samples))

    # ---- 落库写入器（进程内直方图 / 抽检 / 快照的导出出口）----
    #
    # 为什么需要写入器：指标在运行期只进进程内采集器（`MetricsCollector`），而直方图在进程
    # 重启后丢失、跨天对比又必须留着明细，故由 `observability/sampling.py` 的定时任务定期
    # 把累积样本导出到 `eval_run`。写入在这里统一收口，采样侧不直接拼 ORM。

    async def record_node_timing(
        self,
        run_date: date,
        node: str,
        *,
        samples: Sequence[float],
        process_id: str | None = None,
        failures: int = 0,
        degraded: int = 0,
    ) -> int:
        """落一批节点耗时明细（`kind=node_timing`），返回写入行数。

        `failures` / `degraded` 是同一节点在该批里的失败与降级计数，摊到前若干行上：失败率
        与降级率按 `ok` / `degraded` 列聚合，行的粒度为"一次执行"，故计数必须与行对应。
        样本为空则不写（空导出不产生无意义行）。
        """
        if not samples:
            return 0
        rows: list[EvalRun] = []
        for index, value in enumerate(samples):
            rows.append(
                EvalRun(
                    id=new_id("evr"),
                    kind=KIND_NODE_TIMING,
                    run_date=run_date,
                    process_id=process_id,
                    node=node,
                    duration_ms=int(round(value)),
                    ok=index >= failures,
                    degraded=index < degraded,
                    payload={},
                )
            )
        self._session.add_all(rows)
        return len(rows)

    async def record_checkpoint_timing(self, run_date: date, *, samples: Sequence[float]) -> int:
        """落一批检查点写入耗时明细（`kind=checkpoint_timing`），返回写入行数。"""
        if not samples:
            return 0
        rows = [
            EvalRun(
                id=new_id("evr"),
                kind=KIND_CHECKPOINT_TIMING,
                run_date=run_date,
                duration_ms=int(round(value)),
                payload={},
            )
            for value in samples
        ]
        self._session.add_all(rows)
        return len(rows)

    async def insert_quality_samples(self, run_date: date, samples: Sequence[EvalRun]) -> int:
        """写入抽检批次行（`kind=quality_sample`）。

        幂等由 `uq_eval_run_quality_sample` 部分唯一索引兜底：同一天同一条处方卡重复插入会
        被数据库拒绝，调用方（`QualitySampleService`）先查后插避免不必要的冲突。
        """
        for row in samples:
            row.kind = KIND_QUALITY_SAMPLE
            row.run_date = run_date
        self._session.add_all(list(samples))
        return len(samples)

    async def existing_quality_sample_card_ids(self, run_date: date) -> set[str]:
        """当天已入样的处方卡 id 集合，供抽样去重（幂等判定的读侧）。"""
        stmt = select(EvalRun.prescription_card_id).where(
            EvalRun.kind == KIND_QUALITY_SAMPLE,
            EvalRun.run_date == run_date,
            EvalRun.prescription_card_id.is_not(None),
        )
        return {str(card_id) for card_id in (await self._session.execute(stmt)).scalars().all()}

    async def existing_quality_sample_counts(self, run_date: date) -> dict[str, int]:
        """当天已入样的条数，按 `process_id` 分组。

        `insufficient` 的判定基准是"某条流程当日**累计**抽检数是否达到下限"，而非"本次新抽
        了几条"——补跑当天时后者恒小，会把已经达标的流程误报成样本不足。
        """
        stmt = (
            select(EvalRun.process_id, func.count())
            .where(
                EvalRun.kind == KIND_QUALITY_SAMPLE,
                EvalRun.run_date == run_date,
                EvalRun.process_id.is_not(None),
            )
            .group_by(EvalRun.process_id)
        )
        return {str(process_id): int(count) for process_id, count in (await self._session.execute(stmt)).all()}

    async def quality_sample_bad_rate(self, run_date: date) -> RateMetric:
        """某日抽检的"不可用占比"：`quality_label='unusable'` 数 / 已标注数。

        分母只算已标注行（未标注不构成不合格），与回退触发 `should_rollback_prompt` 的口径
        一致（14.5）。分母为 0 时比值为 0.0。
        """
        labeled_stmt = select(EvalRun.quality_label).where(
            EvalRun.kind == KIND_QUALITY_SAMPLE,
            EvalRun.run_date == run_date,
            EvalRun.quality_label.is_not(None),
        )
        labels = list((await self._session.execute(labeled_stmt)).scalars().all())
        unusable = sum(1 for label in labels if label == QUALITY_LABEL_UNUSABLE)
        return RateMetric.of(unusable, len(labels))

    async def unusable_samples(self, run_date: date) -> list[EvalRun]:
        """某日标为"不可用"的抽检行，供回退工单携带两日样本清单（14.5）。"""
        stmt = (
            select(EvalRun)
            .where(
                EvalRun.kind == KIND_QUALITY_SAMPLE,
                EvalRun.run_date == run_date,
                EvalRun.quality_label == QUALITY_LABEL_UNUSABLE,
            )
            .order_by(EvalRun.prescription_card_id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def insert_metrics_snapshot(self, run_date: date, payload: dict) -> EvalRun:
        """写一份指标快照（`kind=metrics_snapshot`），值放 `payload`。"""
        row = EvalRun(id=new_id("evr"), kind=KIND_METRICS_SNAPSHOT, run_date=run_date, payload=payload)
        self._session.add(row)
        return row

    async def insert_rollback_ticket(self, run_date: date, payload: dict) -> EvalRun:
        """写一张提示词回退工单（`kind=rollback_ticket`），两日样本与版本号放 `payload`。

        工单不是自动回退：提示词回退会改变线上行为，必须有人确认（15.5 灰度原则），
        本表只承载"该生成了、内容是什么"。
        """
        row = EvalRun(id=new_id("evr"), kind=KIND_ROLLBACK_TICKET, run_date=run_date, payload=payload)
        self._session.add(row)
        return row


class _RoundsRow:
    """喂给 `over_round_rate` 的最小行形状（协议的形状，不必是 ORM 实例）。"""

    __slots__ = ("rounds", "status")

    def __init__(self, status: str, rounds: int) -> None:
        self.status = status
        self.rounds = rounds


# 从可观测性模块取镜像常量（两侧取值由测试断言一致）。


def _max_rounds() -> int:
    from app.core.config import get_settings

    return int(get_settings().MAX_COLLECT_ROUNDS)


def default_window(days: int = 1, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """默认统计窗口：最近 `days` 个自然日（含今天）。供接口层与调度入口共用一个口径。"""
    moment = now or datetime.now(UTC)
    today = moment.date()
    start = datetime.combine(today, time.min, tzinfo=UTC)
    return start, moment


__all__ = [
    "NodeFailureRow",
    "NodeLatencyRow",
    "Quantile",
    "RateMetric",
    "RoundsDistribution",
    "SnapshotClickMetric",
    "MetricsRepository",
    "default_window",
]
