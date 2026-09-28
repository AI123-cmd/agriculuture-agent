"""建 gate_audit 与 gate_review_ticket 两张闸门复盘表

Revision ID: 0005_gate_audit
Revises: 0004_process_config
Create Date: 2026-09-25

表结构由本迁移显式建出，ORM 侧 `app/models/gate_audit.py` 与
`app/models/gate_review_ticket.py` 只做映射，两者的列、索引、注释必须逐字对齐，
否则 `test_metadata_matches_orm` 会以非空 diff 报出。

**与任务文件的一处刻意偏离**：T2-46 的迁移骨架给 `violations` / `checked_rules` /
`prescription_snapshot` / `shadowed` 都写了 `server_default`，但 ORM 侧只声明了
Python 侧的 `default=`。若迁移建出服务端默认值而 ORM 没有，`compare_server_default=True`
会判定二者不一致并持续生成"删除默认值"的迁移。本仓库既有的 JSONB NOT NULL 列
（`0004_process_config.process_redline.payload`、`0003_event_outbox.payload`）都遵循
"迁移不写服务端默认值、由 Python 侧兜底"的约定，本迁移沿用同一约定，故上述四列不带
`server_default`。`created_at` 走 `server_default=sa.func.now()` 是全局统一的时间口径，
两侧都写。

两条 `CHECK` 约束只在迁移里建、不写进 ORM `__table_args__`：`alembic` 默认不比对
check 约束（比对需显式注册 `checkconstraint_byname` 扩展，见 `alembic/env.py`），
ORM 侧写了反而会成为"看似有约束、实际未被比对"的假象。审计数据的合法性由数据库兜底。

`downgrade` 只 `drop_table`：两表都无外键指向业务表（`session_id` / `user_id` /
`process_id` 是弱引用），审计与工单要在用户注销、配置修订之后仍能保留。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_gate_audit"
down_revision = "0004_process_config"
branch_labels = None
depends_on = None

_AUDIT_TABLE_COMMENT = "闸门判定审计：每次 evaluate 写入一行，供 8.5 的三条复盘用途"
_TICKET_TABLE_COMMENT = "知识条目复核工单：每个 block 事件落一行，供 8.5 第一条复盘用途"


def upgrade() -> None:
    op.create_table(
        "gate_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False, comment="归属用户ID"),
        sa.Column("process_id", sa.String(20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False, comment="allow/degrade/block"),
        sa.Column("violations", postgresql.JSONB(), nullable=False),
        sa.Column("checked_rules", postgresql.JSONB(), nullable=False),
        sa.Column("prescription_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("shadowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('allow','degrade','block')", name="ck_gate_audit_decision"),
        sa.CheckConstraint("char_length(id) = 36", name="ck_gate_audit_id_len"),
        comment=_AUDIT_TABLE_COMMENT,
    )
    # 单列索引：`index=True` 在 ORM 侧生成的就是这些名字，逐字对齐。
    for col in ("session_id", "user_id", "process_id", "decision", "created_at"):
        op.create_index(f"ix_gate_audit_{col}", "gate_audit", [col])
    # 用途一（block 事件复核）与用途二（degrade 率随时间的走势）：按 decision + 时间取数。
    op.create_index("ix_gate_audit_decision_created", "gate_audit", ["decision", "created_at"])
    # 用途三（按流程核对规则覆盖）：按 process_id + decision 过滤，配置变更回归验证用。
    op.create_index("ix_gate_audit_process_decision", "gate_audit", ["process_id", "decision"])

    op.create_table(
        "gate_review_ticket",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("process_id", sa.String(20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("rule_ids", postgresql.JSONB(), nullable=False),
        sa.Column("knowledge_ids", postgresql.JSONB(), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False, comment="allow/degrade/block"),
        sa.Column("status", sa.String(16), nullable=False, comment="pending/in_review/closed"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        comment=_TICKET_TABLE_COMMENT,
    )
    op.create_index("ix_gate_review_ticket_created_at", "gate_review_ticket", ["created_at"])
    op.create_index("ix_gate_review_ticket_status_created", "gate_review_ticket", ["status", "created_at"])
    op.create_index("ix_gate_review_ticket_process_id", "gate_review_ticket", ["process_id"])


def downgrade() -> None:
    op.drop_table("gate_review_ticket")
    op.drop_table("gate_audit")
