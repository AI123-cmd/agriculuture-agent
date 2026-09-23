"""初始化全部表结构（以 ORM 模型为准）

Revision ID: 0001
Revises:
Create Date: 2026-08-30

"""
from alembic import op

from app.db.base import Base
import app.models  # noqa: F401  确保全部模型已注册

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)