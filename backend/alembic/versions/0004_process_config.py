"""建 process_config 与 process_redline 两张流程配置表

Revision ID: 0004_process_config
Revises: 0003_event_outbox
Create Date: 2026-09-24

任务文件（T2-07）里写的编号是 `0002_process_config`，但该编号在 T1-04 完成后已被
`0002_env_reading` 占用，链的实际 head 是 `0003_event_outbox`。新迁移只能接在其后，
故本文件编号为 `0004_process_config`；`app/core/startup_check.py` 的
`EXPECTED_REVISION` 同步改为本编号，否则启动自检与 `check_migrations.sh` 都会失败。

表结构由本迁移显式建出，ORM 侧 `app/models/process_config.py` 只做映射，两者的列、
索引、唯一约束与表注释必须逐字对齐，否则 `test_metadata_matches_orm` 会以非空 diff 报出。

`config_json` 与 `payload` 用 `JSONB` 而非 `JSON`：需要在它们上面做路径查询；
`rule_ids` 是 JSONB 数组，配 `USING gin` 索引以支持 `rule_ids @> '["R-INTERVAL"]'`
这类「反查哪些流程引用了某规则」的查询。

迁移不回填历史数据：首次落库由 `ConfigSyncService.sync_from_seeds` 完成。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_process_config"
down_revision = "0003_event_outbox"
branch_labels = None
depends_on = None

_CONFIG_TABLE_COMMENT = "流程配置表：全局配置，刻意不带 user_id（对所有农户公开只读，无归属概念）"
_REDLINE_TABLE_COMMENT = "流程红线表：与配置同版本，独立成表以便按规则 id 反查"


def upgrade() -> None:
    op.create_table(
        "process_config",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("process_id", sa.String(20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, comment="draft/approved/deprecated"),
        sa.Column("config_json", postgresql.JSONB(), nullable=False, comment="ProcessConfig 的规范化序列化"),
        sa.Column("content_hash", sa.String(64), nullable=False, comment="内容指纹，供热加载比对"),
        sa.Column("source", sa.String(20), nullable=False, comment="seed/manual"),
        sa.Column("note", sa.Text(), nullable=True, comment="修订说明"),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True, comment="为空表示当前生效"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # 唯一约束显式命名：与 ORM 的 UniqueConstraint(name=...) 必须一致，
        # 否则 autogenerate 会判定"约束被改名"并不断生成改名迁移。
        sa.UniqueConstraint("process_id", "config_version", name="uq_process_config_version"),
        comment=_CONFIG_TABLE_COMMENT,
    )
    op.create_index("ix_process_config_process_id", "process_config", ["process_id"])
    # 「当前生效版本」（`effective_to IS NULL`）是最高频查询，本索引让它走索引扫描。
    op.create_index("ix_process_config_active", "process_config", ["process_id", "effective_to"])

    op.create_table(
        "process_redline",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("process_id", sa.String(20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("redline_id", sa.String(30), nullable=False, comment="如 TEA-01"),
        sa.Column("desc", sa.Text(), nullable=False),
        sa.Column("rule_ids", postgresql.JSONB(), nullable=False, comment='["R-INTERVAL", "R-MAX-USE"]'),
        sa.Column("severity", sa.String(10), nullable=False, comment="block / warn"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("process_id", "config_version", "redline_id", name="uq_process_redline"),
        comment=_REDLINE_TABLE_COMMENT,
    )
    op.create_index("ix_process_redline_process_id", "process_redline", ["process_id"])
    # JSONB 数组上的 GIN 索引必须用 op.execute 原样写 DDL：`op.create_index` 的
    # `postgresql_using` 能识别这个算子类，但 op 级别的索引反射比对不认
    # `jsonb_ops` 与默认算子类的差异，写成 DDL 更直白且与 pg_indexes 的最终形态一致。
    op.execute("CREATE INDEX ix_process_redline_rule ON process_redline USING gin (rule_ids)")


def downgrade() -> None:
    op.drop_table("process_redline")
    op.drop_table("process_config")
