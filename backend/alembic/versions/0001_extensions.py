"""启用 pgvector 与 TimescaleDB 扩展、建立 schema 与知识条目表

Revision ID: 0001_ext
Revises:
Create Date: 2026-09-23

为什么扩展的第一步就要做：`Vector` 列类型与 `TIMESTAMPTZ` 的 `now()` 默认值都
依赖扩展先就位；而 `knowledge` / `checkpoint` 两个 schema 一旦缺失，
`search_path` 会被 PostgreSQL 静默忽略，表会落到 `public`，排查成本极高。

`knowledge_item` 表也放在这一支：它落在本迁移刚建出的 `knowledge` schema 下，
且列类型 `vector(1024)` 依赖本迁移刚启用的 `vector` 扩展，二者不可分离。
本项只建列，不建向量索引 —— HNSW / IVFFlat 的参数要按 T2-48 的真实数据量调。
"""

from alembic import op

revision = "0001_ext"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
    op.execute("CREATE SCHEMA IF NOT EXISTS knowledge")
    op.execute("CREATE SCHEMA IF NOT EXISTS checkpoint")

    op.execute(
        """
        CREATE TABLE knowledge.knowledge_item (
            id          VARCHAR(36)  PRIMARY KEY,
            process_id  VARCHAR(20)  NOT NULL,
            category    VARCHAR(30)  NOT NULL,
            region_code VARCHAR(12),
            status      VARCHAR(20)  NOT NULL,
            title       VARCHAR(200) NOT NULL,
            content     TEXT         NOT NULL,
            facts       JSONB,
            embedding   vector(1024),
            create_time TIMESTAMPTZ  NOT NULL DEFAULT now(),
            update_time TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("COMMENT ON COLUMN knowledge.knowledge_item.create_time IS '创建时间'")
    op.execute("COMMENT ON COLUMN knowledge.knowledge_item.update_time IS '更新时间'")
    op.execute("CREATE INDEX ix_knowledge_item_process_id ON knowledge.knowledge_item (process_id)")
    op.execute("CREATE INDEX ix_knowledge_item_region_code ON knowledge.knowledge_item (region_code)")
    op.execute("CREATE INDEX ix_knowledge_item_status ON knowledge.knowledge_item (status)")


def downgrade() -> None:
    # 扩展不随迁移回滚：`CREATE EXTENSION` 是实例级对象，回滚一个库的迁移不应
    # 影响同实例的其它数据库（其它库可能在用 vector 或 timescaledb）。
    # 这里刻意不写 `DROP EXTENSION`，后来者请勿"补全"，否则会造成跨库破坏。
    op.execute("DROP SCHEMA IF EXISTS checkpoint CASCADE")
    op.execute("DROP SCHEMA IF EXISTS knowledge CASCADE")
