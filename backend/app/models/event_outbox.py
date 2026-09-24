"""事务性发件箱模型（技术方案 6.6 事件驱动协同）。

发件箱是"业务事务内先落事件、独立进程再投递"的落点：`emit` 只把行 `add` 进
当前事务，不提交，因此业务回滚时事件一并消失，不会留下"有事件无数据"的脏记录。

`status` 取三值而非布尔：`dead` 表达"重试耗尽需人工介入"，它必须能被查出来，
否则失败会被静默吞掉。`payload` 用 `JSONB` 而非 `JSON`，因为排查时要按
`payload->>'xxx'` 做查询统计。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 投递状态三值。`dead` 是第三态而不是布尔里的"未投递"：
# 它要能与"还在重试"区分开，否则运维无从知道哪些事件需要人工处理。
STATUS_PENDING = "pending"
STATUS_DISPATCHED = "dispatched"
STATUS_DEAD = "dead"


class EventOutbox(Base):
    """待投递领域事件。

    `idempotency_key` 唯一约束是本表的第二重保险：除了防重复 `emit`，它还让
    "同一处方重复发事件"这类 bug 在写入阶段就暴露，而不是等消费者处理两遍。
    """

    __tablename__ = "event_outbox"
    __table_args__ = (
        # 显式命名唯一约束：交给 SQLAlchemy 自动命名会得到带 `_key` 后缀的名字，
        # 与迁移 DDL 对不上时 autogenerate 会不断生成无意义的改名迁移。
        UniqueConstraint("idempotency_key", name="uq_event_outbox_idempotency_key"),
        # 投递查询的主索引，也是本表唯一的高频查询：`status='pending' AND available_at <= now()`。
        # 此处偏离任务骨架一处：骨架给 `status` 标了 `index=True`，即再建一条单列索引，
        # 但 `status` 是复合索引的前缀，单列索引完全冗余，只会给每次插入增加一次写放大。
        Index("ix_event_outbox_status_available_at", "status", "available_at"),
        # 部分索引：只含 `pending` 行，已投递的 `dispatched` 行不占这份索引，
        # 因此索引大小与"待投递量"成正比，与历史总量无关。
        Index("ix_event_outbox_pending", "available_at", postgresql_where=text("status = 'pending'")),
        # 以下三条用于排查与统计，不参与投递查询
        Index("ix_event_outbox_event_type", "event_type"),
        Index("ix_event_outbox_aggregate_id", "aggregate_id"),
        Index("ix_event_outbox_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, comment="如 prescription.created")
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False, comment="聚合根类型，如 prescription")
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="聚合根ID")
    # 可空：并非所有事件都归属于某个用户（如系统级定时事件）
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="事件载荷")
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, comment="默认 event_type:aggregate_id")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="已投递尝试次数")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最近一次投递失败原因")
    # `available_at` 既是"最早可投递时刻"（退避用），也是抢占租约（见 events/outbox.py）
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
