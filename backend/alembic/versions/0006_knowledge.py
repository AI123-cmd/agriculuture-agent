"""建 knowledge_item 与 knowledge_region 两张知识库表（T2-47，技术方案 9.2）

Revision ID: 0006_knowledge
Revises: 0005_gate_audit
Create Date: 2026-09-29

任务文件（T2-47）里写的编号是 `0003_knowledge`，但该编号在 T1-04 完成后已被
`0003_event_outbox` 占用，链的实际 head 是 `0005_gate_audit`。新迁移只能接在其后，
故本文件编号为 `0006_knowledge`；`app/core/startup_check.py` 的 `EXPECTED_REVISION`
同步改为本编号，否则启动自检与 `check_migrations.sh` 都会失败。

两张表落在 `knowledge` schema 下（由 `0001_ext` 建出）。表结构由本迁移显式建出，
ORM 侧 `app/models/knowledge.py` 只做映射，两者的列、索引、唯一约束、CHECK 约束与
表注释必须逐字对齐，否则 `test_metadata_matches_orm` 会以非空 diff 报出。

**与任务文件的一处刻意偏离**：T2-47 的迁移要点把 HNSW 索引称为"不放进 `__table_args__`"。
本仓库的 alembic 按"索引名是否在 ORM 元数据中"判断增删（`postgresql_using` /
`postgresql_with` / `postgresql_ops` 均不参与比对，见 `alembic/ddl/postgresql.py` 的
`compare_indexes`），若 ORM 不声明该索引名，每次 autogenerate 都会误报"删除 HNSW 索引"。
因此 ORM 侧声明了同名 `Index`（仅名字与列，不比对参数），参数（m / ef_construction）与
访问方法仍只由本迁移的 DDL 决定。`ck_knowledge_*` 两条 CHECK 约束同样只在迁移里建、
不写进 ORM `__table_args__`——alembic 默认不比对 check 约束，ORM 写了反而成为
"看似有约束、实际未被比对"的假象（沿用 `0005_gate_audit` 的既有约定）。

`downgrade` 按"先 drop 索引、再 drop 表、最后不动扩展"的顺序回退：`vector` 扩展由
`0001_ext` 负责生命周期，本迁移不 drop。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0006_knowledge"
down_revision = "0005_gate_audit"
branch_labels = None
depends_on = None

_SCHEMA = "knowledge"
_ITEM_TABLE_COMMENT = "知识条目：四层知识共用一张表，以 kind 区分；闸门比对基准"
_REGION_TABLE_COMMENT = "知识条目地区适用范围：一条条目可覆盖多个行政区，支持分级回退匹配"

# HNSW 索引单独用原始 DDL 建：`op.create_index` 能识别 `postgresql_using`，但
# `WITH (m=..., ef_construction=...)` 不在其支持范围内，写 DDL 更直白且与 pg_indexes 形态一致。
_HNSW_INDEX_DDL = (
    f"CREATE INDEX ix_knowledge_embedding_hnsw ON {_SCHEMA}.knowledge_item "
    "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
)


def upgrade() -> None:
    # 知识条目依赖 pgvector 的 `vector` 类型；T1-03 已建，这里再兜底一次保证本迁移可独立执行。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_item",
        sa.Column("knowledge_id", sa.String(36), primary_key=True),
        sa.Column("process_id", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, comment="disease/pest/pesticide/cultivation/threshold"),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, comment="正文，用于检索与改写"),
        # 药剂类字段：独立成列以便白名单/剂量/间隔期规则精确查，不从正文模糊匹配。
        sa.Column("pesticide_name", sa.String(80), nullable=True, comment="药剂名，白名单/冲突规则精确查"),
        sa.Column("registered_crops", postgresql.JSONB(), nullable=False),
        sa.Column("dosage_min", sa.Numeric(12, 4), nullable=True),
        sa.Column("dosage_max", sa.Numeric(12, 4), nullable=True),
        sa.Column("dosage_unit", sa.String(16), nullable=True, comment="克/亩、毫升/亩、倍液"),
        sa.Column("safety_interval_days", sa.Integer(), nullable=True),
        sa.Column("max_uses_per_season", sa.Integer(), nullable=True),
        sa.Column("forbidden_combinations", postgresql.JSONB(), nullable=False),
        # 适用范围与生效期。
        sa.Column("region_scope", sa.String(12), nullable=True, comment="行政区编码前缀，NULL 表示全国"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expire_date", sa.Date(), nullable=True),
        # 状态与来源。`status_reason` 在 suspended 时强制非空，由 CHECK 约束保证。
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True, comment="suspended 的理由必填"),
        sa.Column("source_ref", sa.Text(), nullable=True, comment="权威来源引用"),
        sa.Column("embedding", Vector(1024), nullable=True, comment="pgvector 列，维度与 EMBEDDING_DIM 一致"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind != 'pesticide' OR dosage_min IS NULL OR dosage_max IS NULL OR dosage_min <= dosage_max",
            name="ck_knowledge_dosage_range",
        ),
        sa.CheckConstraint("status != 'suspended' OR status_reason IS NOT NULL", name="ck_knowledge_suspended_reason"),
        comment=_ITEM_TABLE_COMMENT,
        schema=_SCHEMA,
    )
    # 单列与组合索引：名字与 ORM 显式声明逐字对齐。
    op.create_index("ix_knowledge_item_process_id", "knowledge_item", ["process_id"], schema=_SCHEMA)
    op.create_index("ix_knowledge_item_kind", "knowledge_item", ["kind"], schema=_SCHEMA)
    op.create_index("ix_knowledge_item_status", "knowledge_item", ["status"], schema=_SCHEMA)
    op.create_index("ix_knowledge_item_pesticide_name", "knowledge_item", ["pesticide_name"], schema=_SCHEMA)
    op.create_index("ix_knowledge_region_scope", "knowledge_item", ["region_scope"], schema=_SCHEMA)
    op.create_index("ix_knowledge_process_status_kind", "knowledge_item", ["process_id", "status", "kind"], schema=_SCHEMA)
    op.execute(_HNSW_INDEX_DDL)

    op.create_table(
        "knowledge_region",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_id", sa.String(36), nullable=False),
        sa.Column("region_code", sa.String(12), nullable=False, comment="行政区编码"),
        sa.Column("region_level", sa.String(10), nullable=False, comment="county/city/province/national"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("knowledge_id", "region_code", name="uq_knowledge_region"),
        comment=_REGION_TABLE_COMMENT,
        schema=_SCHEMA,
    )
    op.create_index("ix_knowledge_region_knowledge_id", "knowledge_region", ["knowledge_id"], schema=_SCHEMA)
    op.create_index("ix_knowledge_region_region_code", "knowledge_region", ["region_code"], schema=_SCHEMA)
    op.create_index(
        "ix_knowledge_region_code_level", "knowledge_region", ["region_code", "region_level"], schema=_SCHEMA
    )


def downgrade() -> None:
    # 先 drop 表（索引随表一起删），不动扩展：`vector` 由 0001_ext 负责生命周期。
    op.drop_table("knowledge_region", schema=_SCHEMA)
    op.drop_table("knowledge_item", schema=_SCHEMA)
