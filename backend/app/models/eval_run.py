"""评测运行与指标快照表（T6-09，技术方案 12.1 的 `eval_run`）。

一张表承载 T6-09 的四类记录，靠 `kind` 区分，而不是各建一张小表：

1. `node_timing`：各节点的单次耗时与成败（14.3 链路级指标"各节点耗时 P50/P95""失败率"）。
   P50/P95 用 SQL 的 `percentile_cont` 从这些明细算——**不用进程内直方图**：直方图在进程
   重启后丢失，而"某次发版后 P95 劣化"通常需要跨天对比（14.3 对检查点成本的同一判断）。
2. `checkpoint_timing`：检查点写入耗时（`observe_checkpoint_write_ms` 落库后的样本）。
3. `quality_sample`：每日人工抽检批次的一行，幂等键 `(run_date, prescription_card_id)`。
4. `metrics_snapshot` / `rollback_ticket`：指标快照与提示词回退工单（工单内容放 `payload`）。

**为什么不带 `user_id`**：这是平台侧的观测数据（与 `llm_call` / `gate_review_ticket` 同类），
不承载归属语义；`prescription_card_id` 是弱引用，刻意不建外键——卡片是业务数据，指标要能在
数据清理之后仍可复盘。故本表不走 `OwnedRepository`，由 `MetricsRepository` 以会话绑定形态访问。

`kind` / `quality_label` 用 `String` 列而非 PostgreSQL 原生 ENUM：新增观测维度只改代码不动表
（全局约定第四节"枚举封闭"，与 `llm_call.agent`、`gate_audit.decision` 的口径一致）。
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_TABLE_COMMENT = "评测运行与指标快照：节点耗时、检查点耗时、抽检批次与工单"

# `kind` 的取值闭集。放模块常量而不散落字符串：写入方与查询方共用一份，拼错即报错。
KIND_NODE_TIMING = "node_timing"
KIND_CHECKPOINT_TIMING = "checkpoint_timing"
KIND_QUALITY_SAMPLE = "quality_sample"
KIND_METRICS_SNAPSHOT = "metrics_snapshot"
KIND_ROLLBACK_TICKET = "rollback_ticket"

# 人工抽检三值（14.5）。三值而非二值：`需修正` 进提示词迭代队列，`不可用` 才可能触发回退，
# 合并成一个"不合格"会丢掉这个区分。
QUALITY_LABEL_USABLE = "usable"
QUALITY_LABEL_NEEDS_FIX = "needs_fix"
QUALITY_LABEL_UNUSABLE = "unusable"


class EvalRun(Base):
    """评测运行与指标快照。`kind` 决定一行承载哪类记录。"""

    __tablename__ = "eval_run"
    __table_args__ = (
        # 指标查询主索引：按"类别 + 自然日"取数（每日看板、跨天对比）。
        Index("ix_eval_run_kind_date", "kind", "run_date"),
        # 链路级耗时按节点分组算 P50/P95。
        Index("ix_eval_run_node_date", "node", "run_date"),
        # 按流程切分耗时走势（定位"某条流程变慢"）。
        Index("ix_eval_run_process_date", "process_id", "run_date"),
        # 抽检幂等键：同一天同一处方卡只入样一次，补跑不产生重复行。
        # 部分唯一索引（只约束 quality_sample 行）：其余 kind 的 prescription_card_id 为空，
        # 全表唯一约束对多行 NULL 不去重，故必须限定 kind。
        Index(
            "uq_eval_run_quality_sample",
            "run_date",
            "prescription_card_id",
            unique=True,
            postgresql_where=text("kind = 'quality_sample'"),
        ),
        {"comment": _TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 记录类别：node_timing / checkpoint_timing / quality_sample / metrics_snapshot / rollback_ticket
    kind: Mapped[str] = mapped_column(String(30), nullable=False, comment="记录类别")
    run_date: Mapped[date] = mapped_column(Date, nullable=False, comment="归属自然日，跨天对比的分组键")
    process_id: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="七条流程之一")
    # 链路级耗时（node_timing / checkpoint_timing）。
    node: Mapped[str | None] = mapped_column(String(60), nullable=True, comment="节点名（span 名）")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时，毫秒")
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="节点是否成功（node_timing）")
    degraded: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="是否发生降级/重试（node_timing 的 llm.degraded）"
    )
    # 人工抽检（quality_sample）。label 与 card 上的列是同一事实的两处落点：卡片列供业务侧
    # 反查，批次行供看板按天聚合，写入时一并更新。
    prescription_card_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="抽检样本处方卡")
    quality_label: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="usable/needs_fix/unusable，未标注为空"
    )
    quality_labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 分流准确率的人工标注位（14.3 第一项）：抽检时判定该样本的 process_id 是否正确。
    # 代码库无处存放"分诊正确答案"，故由抽检标注补齐；未标注为空，不参与分母。
    triage_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="分流是否正确（人工抽检）")
    # 结构化附加内容：metrics_snapshot 的指标值、rollback_ticket 的两日样本清单与提示词版本号。
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = [
    "KIND_CHECKPOINT_TIMING",
    "KIND_METRICS_SNAPSHOT",
    "KIND_NODE_TIMING",
    "KIND_QUALITY_SAMPLE",
    "KIND_ROLLBACK_TICKET",
    "QUALITY_LABEL_NEEDS_FIX",
    "QUALITY_LABEL_UNUSABLE",
    "QUALITY_LABEL_USABLE",
    "EvalRun",
]
