"""建 knowledge_review_log 知识条目状态变更记录表（T2-54，技术方案 9.2）

Revision ID: 0007_knowledge_review
Revises: 0006_knowledge
Create Date: 2026-09-29

任务文件（T2-54）里写的编号是 `0004_knowledge_review`，但该编号在 T1-04 完成后已被
`0004_process_config` 占用，链的实际 head 是 `0006_knowledge`。新迁移只能接在其后，
故本文件编号为 `0007_knowledge_review`；`app/core/startup_check.py` 的
`EXPECTED_REVISION` 同步改为本编号，否则启动自检与 `check_migrations.sh` 都会失败。

表落在 `knowledge` schema 下（由 `0001_ext` 建出），与 `knowledge_item` /
`knowledge_region` 同域。表结构由本迁移显式建出，ORM 侧 `app/models/knowledge_review.py`
只做映射，两者的列与索引必须逐字对齐，否则 `test_metadata_matches_orm` 会以非空 diff
报出。

**不可变性由结构表达**：本表无 `updated_at` 列，迁移也不建任何更新触发器；配合仓储只提供
追加与查询方法，即"变更记录只追加"。若某次操作填错，正确做法是追加一条纠正记录，而不是
修改历史行。

`downgrade` 直接 drop 表（索引随表一起删）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_knowledge_review"
down_revision = "0006_knowledge"
branch_labels = None
depends_on = None

_SCHEMA = "knowledge"
_TABLE_COMMENT = "知识条目状态变更记录：只追加，不修改，不删除"


def upgrade() -> None:
    op.create_table(
        "knowledge_review_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_id", sa.String(36), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=False),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column(
            "action",
            sa.String(20),
            nullable=False,
            comment="submit/approve/reject/suspend/resume/deprecate",
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("operator_id", sa.String(36), nullable=False, comment="后台账号 id，弱引用不建外键"),
        sa.Column("consulted_ref", sa.String(36), nullable=True, comment="关联的问诊，若因某次问诊触发"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        comment=_TABLE_COMMENT,
        schema=_SCHEMA,
    )
    # 索引名与 ORM 显式声明逐字对齐。
    op.create_index("ix_review_log_knowledge_id", "knowledge_review_log", ["knowledge_id"], schema=_SCHEMA)
    op.create_index("ix_review_log_operator_id", "knowledge_review_log", ["operator_id"], schema=_SCHEMA)
    op.create_index("ix_review_log_created_at", "knowledge_review_log", ["created_at"], schema=_SCHEMA)
    op.create_index(
        "ix_review_log_kid_created",
        "knowledge_review_log",
        ["knowledge_id", "created_at"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    # 先 drop 表（索引随表一起删）。
    op.drop_table("knowledge_review_log", schema=_SCHEMA)
