"""给处方卡补回访计划与下一轮采摘日期（T3-09 前置）

Revision ID: 0012_prescription_card_followup
Revises: 0011_prompt_item_rule_id_width
Create Date: 2026-10-01

`0010_consultation` 按技术方案 12.4 建出 `prescription_card` 时未给"回访计划"与"下一轮采摘
计划日期"两列。T3-09（回访调度服务）登记回访任务时要读 `card.followup_plan`（T2-21 产出，
`tasks` 内含 `due_offset_days` / `kind` / `ask` / `content_rule` / `window_based`）与
`card.next_pick_date`（茶流程计算安全间隔期结束与下一轮采摘的较晚者）。按"逻辑以新文档为主"
就地补列，不改动既有列。

两列都放开可空：`followup_plan` 在转人工降级等无回访计划的处方上为空，`next_pick_date` 仅
茶流程有值。`downgrade` 逆序 drop 两列即可，下游无引用本列的结构，无需级联改动。列名、类型、
可空性与注释须与 ORM 侧 `app/models/consultation.py` 的 `PrescriptionCard` 逐字对齐，否则
`test_metadata_matches_orm` 会以非空 diff 报出。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_prescription_card_followup"
down_revision = "0011_prompt_item_rule_id_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prescription_card",
        sa.Column("followup_plan", postgresql.JSONB(), nullable=True, comment="回访计划，来自处方契约"),
    )
    op.add_column(
        "prescription_card",
        sa.Column("next_pick_date", sa.Date(), nullable=True, comment="下一轮采摘计划日期（茶）"),
    )


def downgrade() -> None:
    op.drop_column("prescription_card", "next_pick_date")
    op.drop_column("prescription_card", "followup_plan")
