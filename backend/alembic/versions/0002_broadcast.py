"""扩展 broadcast_log：新增 level/source/meta 字段

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("broadcast_log")]

    if "level" not in columns:
        op.add_column(
            "broadcast_log",
            sa.Column("level", sa.String(10), nullable=False, server_default="normal", comment="danger/warning/normal"),
        )
    if "source" not in columns:
        op.add_column(
            "broadcast_log",
            sa.Column("source", sa.String(20), nullable=False, server_default="sensor", comment="sensor/weather/calendar/ai"),
        )
    if "meta" not in columns:
        op.add_column(
            "broadcast_log",
            sa.Column("meta", sa.JSON(), nullable=True, comment="触发该条播报的环境快照"),
        )


def downgrade() -> None:
    for col in ("meta", "source", "level"):
        op.drop_column("broadcast_log", col)