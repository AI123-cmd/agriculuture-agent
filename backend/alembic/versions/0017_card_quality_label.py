"""为处方卡加人工抽检标注列（T6-09）

Revision ID: 0017_card_quality_label
Revises: 0016_eval_run
Create Date: 2026-10-12

T6-09（技术方案 14.5）的人工抽检需要把标注结论落到处方卡上：`quality_label` 三值
（usable / needs_fix / unusable）与 `quality_labeled_at` 标注时点。两列均可空——未抽检
到的卡片为空。

为什么同一事实也写一份到 `eval_run`：看板按"自然日 + 流程"聚合走 `eval_run`（`run_date`
是分组键），而业务侧"这张卡被标注过什么"按卡片反查，故两处一并写。卡片列是业务视图，
`eval_run` 行是统计视图。

列顺序与注释与 `app/models/consultation.py` 的 `PrescriptionCard` 逐字对齐。
revision 串刻意短于 32 字符：`alembic_version.version_num` 是 `VARCHAR(32)`，超长会在
`UPDATE` 时抛 `StringDataRightTruncation`。
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_card_quality_label"
down_revision = "0016_eval_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prescription_card",
        sa.Column("quality_label", sa.String(20), nullable=True, comment="usable/needs_fix/unusable，未标注为空"),
    )
    op.add_column(
        "prescription_card",
        sa.Column("quality_labeled_at", sa.DateTime(timezone=True), nullable=True, comment="标注时点"),
    )


def downgrade() -> None:
    op.drop_column("prescription_card", "quality_labeled_at")
    op.drop_column("prescription_card", "quality_label")
