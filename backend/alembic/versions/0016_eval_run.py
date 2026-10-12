"""建评测运行与指标快照表（T6-09）

Revision ID: 0016_eval_run
Revises: 0015_vendor_template
Create Date: 2026-10-12

按技术方案 12.1 新建 `eval_run`，承载 T6-09 的四类记录（`kind` 区分）：节点耗时、
检查点写入耗时、每日抽检批次、指标快照与回退工单。

两处刻意设计：

- **不带 `user_id`**：平台侧观测数据（与 `llm_call` / `gate_review_ticket` 同类），不承载归属。
- **抽检幂等键用部分唯一索引**：`(run_date, prescription_card_id) WHERE kind = 'quality_sample'`。
  用普通唯一约束不行——其余 kind 行的 `prescription_card_id` 为空，而 PostgreSQL 唯一约束对
  多行 NULL 不去重，普通约束拦不住"同日同卡重复入样"，也管不到别的 kind。

列、索引、注释与 `app/models/eval_run.py` 逐字对齐（`test_metadata_matches_orm` 会比对）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_eval_run"
down_revision = "0015_vendor_template"
branch_labels = None
depends_on = None

_TABLE_COMMENT = "评测运行与指标快照：节点耗时、检查点耗时、抽检批次与工单"


def upgrade() -> None:
    op.create_table(
        "eval_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False, comment="记录类别"),
        sa.Column("run_date", sa.Date(), nullable=False, comment="归属自然日，跨天对比的分组键"),
        sa.Column("process_id", sa.String(20), nullable=True, comment="七条流程之一"),
        sa.Column("node", sa.String(60), nullable=True, comment="节点名（span 名）"),
        sa.Column("duration_ms", sa.Integer(), nullable=True, comment="耗时，毫秒"),
        sa.Column("ok", sa.Boolean(), nullable=True, comment="节点是否成功（node_timing）"),
        sa.Column(
            "degraded",
            sa.Boolean(),
            nullable=True,
            comment="是否发生降级/重试（node_timing 的 llm.degraded）",
        ),
        sa.Column("prescription_card_id", sa.String(36), nullable=True, comment="抽检样本处方卡"),
        sa.Column("quality_label", sa.String(20), nullable=True, comment="usable/needs_fix/unusable，未标注为空"),
        sa.Column("quality_labeled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triage_correct", sa.Boolean(), nullable=True, comment="分流是否正确（人工抽检）"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment=_TABLE_COMMENT,
    )
    op.create_index("ix_eval_run_kind_date", "eval_run", ["kind", "run_date"])
    op.create_index("ix_eval_run_node_date", "eval_run", ["node", "run_date"])
    op.create_index("ix_eval_run_process_date", "eval_run", ["process_id", "run_date"])
    op.create_index(
        "uq_eval_run_quality_sample",
        "eval_run",
        ["run_date", "prescription_card_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'quality_sample'"),
    )


def downgrade() -> None:
    op.drop_index("uq_eval_run_quality_sample", table_name="eval_run")
    op.drop_index("ix_eval_run_process_date", table_name="eval_run")
    op.drop_index("ix_eval_run_node_date", table_name="eval_run")
    op.drop_index("ix_eval_run_kind_date", table_name="eval_run")
    op.drop_table("eval_run")
