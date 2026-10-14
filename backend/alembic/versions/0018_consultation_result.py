"""给问诊会话加统一"问诊结果"快照列

Revision ID: 0018_consultation_result
Revises: 0017_card_quality_label
Create Date: 2026-10-14

转人工路径此前不落 `consultation` 行、历史回看无任何结果。改造后所有终态路径（出方 /
转人工 / 安全拦截 / 法定报告）都写一份统一结构的"问诊结果"快照，历史回看据此还原结果卡。
出方路径的处置明细仍落在 `prescription_card`，本列承载的是对外的结果视图（结论、已了解情况、
观察要点、下一步建议等）。

列可空性为 `NOT NULL` 且不写 `server_default`：与本仓库既有 JSONB 列口径一致，由 ORM 侧
`default=dict` 兜底，避免 `compare_server_default=True` 生成假迁移。列名、类型、可空性与注释
须与 `app/models/consultation.py` 的 `Consultation` 逐字对齐，否则 `test_metadata_matches_orm`
会以非空 diff 报出。存量行由数据库在 `ADD COLUMN` 时按 `NOT NULL` 语义填充空对象。

revision 串刻意短于 32 字符：`alembic_version.version_num` 是 `VARCHAR(32)`。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_consultation_result"
down_revision = "0017_card_quality_label"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consultation",
        sa.Column(
            "result_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="问诊结果快照，所有终态路径都写",
        ),
    )
    # 存量行已由 server_default 填好空对象，随后撤掉默认值：本仓库口径是 JSONB 列不写
    # server_default（由 ORM 的 default=dict 兜底），保留默认值会让 compare_server_default
    # 持续生成"删除默认值"的假迁移。
    op.alter_column("consultation", "result_snapshot", server_default=None)


def downgrade() -> None:
    op.drop_column("consultation", "result_snapshot")
