"""启用 pgvector 与 TimescaleDB 扩展、建立 schema

Revision ID: 0001_ext
Revises:
Create Date: 2026-09-23

为什么扩展的第一步就要做：`Vector` 列类型与 `TIMESTAMPTZ` 的 `now()` 默认值都
依赖扩展先就位；而 `knowledge` / `checkpoint` 两个 schema 一旦缺失，
`search_path` 会被 PostgreSQL 静默忽略，表会落到 `public`，排查成本极高。

**本迁移只负责实例级对象与 schema，不再建 `knowledge_item`**：知识条目的表结构由
T2-47 的迁移（`0006_knowledge`）按技术方案 9.2 的字段清单显式建出，两张表
（`knowledge_item` / `knowledge_region`）与 HNSW 向量索引都在那里一次落地。
原先把一张占位表塞进本迁移，会让"表结构"散落两处、`compare_metadata` 的归因变模糊。
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


def downgrade() -> None:
    # 扩展不随迁移回滚：`CREATE EXTENSION` 是实例级对象，回滚一个库的迁移不应
    # 影响同实例的其它数据库（其它库可能在用 vector 或 timescaledb）。
    # 这里刻意不写 `DROP EXTENSION`，后来者请勿"补全"，否则会造成跨库破坏。
    op.execute("DROP SCHEMA IF EXISTS checkpoint CASCADE")
    op.execute("DROP SCHEMA IF EXISTS knowledge CASCADE")
