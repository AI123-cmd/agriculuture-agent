"""建 event_outbox 事务性发件箱表

Revision ID: 0003_event_outbox
Revises: 0002_env_reading
Create Date: 2026-09-23

表结构由本迁移显式建出（T1-04 之后不再有 `create_all`），ORM 侧
`app/models/event_outbox.py` 只做映射，两者的列、索引、唯一约束必须逐字对齐，
否则 `test_metadata_matches_orm` 会以非空 diff 报出。

三类索引各有明确用途，不是"多建几条保险"：

1. `ix_event_outbox_status_available_at` —— 投递查询
   （`status='pending' AND available_at <= now()`）的主索引，也是本表唯一的高频查询。
2. `ix_event_outbox_pending` —— 部分索引，只含 `pending` 行。已投递的 `dispatched`
   行不占这份索引，索引大小因而与"待投递量"成正比、与历史总量无关。
3. `ix_event_outbox_event_type` / `_aggregate_id` / `_user_id` —— 排查与统计用，
   不参与投递路径。

刻意不建 `status` 单列索引：它是复合索引 (1) 的前缀，单列版本完全冗余。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_event_outbox"
down_revision = "0002_env_reading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(60), nullable=False, comment="如 prescription.created"),
        sa.Column("aggregate_type", sa.String(40), nullable=False, comment="聚合根类型，如 prescription"),
        sa.Column("aggregate_id", sa.String(36), nullable=False, comment="聚合根ID"),
        # 可空：系统级事件（如定时产生的 followup.due）不归属任何用户
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, comment="事件载荷"),
        sa.Column("idempotency_key", sa.String(80), nullable=False, comment="默认 event_type:aggregate_id"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, comment="已投递尝试次数"),
        sa.Column("last_error", sa.Text(), nullable=True, comment="最近一次投递失败原因"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        # 唯一约束显式命名：与 ORM 的 UniqueConstraint(name=...) 必须一致，
        # 否则 autogenerate 会判定"约束被改名"并不断生成改名迁移。
        sa.UniqueConstraint("idempotency_key", name="uq_event_outbox_idempotency_key"),
    )

    op.create_index("ix_event_outbox_status_available_at", "event_outbox", ["status", "available_at"])
    op.create_index("ix_event_outbox_event_type", "event_outbox", ["event_type"])
    op.create_index("ix_event_outbox_aggregate_id", "event_outbox", ["aggregate_id"])
    op.create_index("ix_event_outbox_user_id", "event_outbox", ["user_id"])
    # 部分索引必须用 op.execute 原样写 DDL：`op.create_index` 的
    # `postgresql_where` 参数在 Alembic 的索引反射比对里能识别，但条件文本
    # （`(status)::text = 'pending'::text`）由服务端规范化后回读，
    # 写成 DDL 更直白，且与 pg_indexes 里的最终形态一致。
    op.execute("CREATE INDEX ix_event_outbox_pending ON event_outbox (available_at) WHERE status = 'pending'")


def downgrade() -> None:
    op.drop_table("event_outbox")
