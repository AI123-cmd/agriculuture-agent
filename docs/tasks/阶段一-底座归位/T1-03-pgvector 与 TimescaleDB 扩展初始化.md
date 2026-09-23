# T1-03 pgvector 与 TimescaleDB 扩展初始化

## 这一项要做什么

在目标 PostgreSQL 实例上启用 `vector` 与 `timescaledb` 两个扩展，建立知识检索所需的向量列与索引类型，并把 `env_reading` 建成 TimescaleDB 超表。知识库混合检索（T2-49）与设备读数落库（T5-07）都依赖这一步，且超表一旦建成再改分区与主键的代价很高，所以必须在第一次迁移之前一次性把结构定对。

这一步只做扩展、超表与向量列的基础设施；知识条目的业务字段与索引策略在 T2-47/T2-48 落地。

## 前置依赖

- 依赖 T1-02 已提供：可用的异步引擎、`DATABASE_URL` 与 `CHECKPOINT_DB_URL`、`psycopg` 驱动。
- 外部环境：PostgreSQL 16+ **镜像必须自带扩展**。`pgvector/pgvector` 与 `timescale/timescaledb-ha` 是常见做法；若用 `postgres:16-alpine` 官方镜像，两者都不含，必须先换镜像。
- 外部环境：执行迁移的账号具备 `CREATE EXTENSION` 权限（通常是实例超级用户或 `rds_superuser` 等价角色）。
- 需要人工确认：知识条目向量的维度（与 T2-56 选定的 embedding 模型输出维度一致，常见 1024 或 1536）。本项按 1024 建列，切换模型时需重建列与索引。

## 参考章节

- 技术方案 第4章 4.2 完整技术栈（pgvector 0.7+、TimescaleDB 为 PG 扩展）
- 技术方案 第4章 4.3 为什么是 PostgreSQL 统一栈（第三条：时序量在 TimescaleDB 能力范围内）
- 技术方案 第12章 12.6 环境数据三表（`env_reading` 建表 SQL 与 `create_hypertable`）
- 技术方案 第12章 12.1 表清单总览（`knowledge_item` / `env_reading`）
- 技术方案 第9章 9.3 混合检索实现（向量列与元数据过滤同语句）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/alembic/versions/0001_extensions.py` | 建 `vector` / `timescaledb` 扩展与 `knowledge`、`checkpoint` schema |
| 新增 | `backend/alembic/versions/0002_env_reading_hypertable.py` | 建 `env_reading` 并与超表、索引 |
| 新增 | `backend/app/models/env_reading.py` | ORM 映射（`BIGSERIAL` 主键 + 复合主键 `id, observed_at`） |
| 新增 | `backend/app/models/knowledge.py` | `KnowledgeItem`，含 `embedding Vector(1024)` |
| 新增 | `backend/scripts/init_extensions.sql` | 供 DBA 手工预置的等价 SQL（与迁移同内容） |
| 修改 | `backend/app/models/__init__.py` | 注册两个新模型 |
| 新增 | `backend/tests/test_extensions.py` | 断言扩展存在、超表已建、向量列可比较 |
| 新增 | `backend/tests/sql/verify_extensions.sql` | 人工核对用的查询脚本 |

## 实现要点

### 一、扩展初始化放在第一支迁移

扩展与 schema 必须先于任何建表存在，否则 `Vector` 列与 `TIMESTAMPTZ` 默认值都会失败。放在 `0001_extensions.py`：

```python
"""启用 pgvector 与 TimescaleDB 扩展、建立 schema

Revision ID: 0001_ext
Revises:
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
    # 扩展不随迁移回滚，避免误删影响同实例其它库
    op.execute("DROP SCHEMA IF EXISTS checkpoint CASCADE")
    op.execute("DROP SCHEMA IF EXISTS knowledge CASCADE")
```

`downgrade` 刻意不回滚 `CREATE EXTENSION`：扩展是实例级对象，回滚迁移不应影响同实例的其它数据库。这一点要写在迁移文件注释里，避免后来者"补全"回滚逻辑。

### 二、env_reading 超表

按技术方案 12.6 的建表语句，用 `op.execute` 原样执行，因为 Alembic 的 `op.create_table` 不识别 `create_hypertable`：

```python
def upgrade() -> None:
    op.execute("""
        CREATE TABLE env_reading (
            id            BIGSERIAL,
            user_id       VARCHAR(36)   NOT NULL,
            device_id     VARCHAR(36),
            farm_entry_id VARCHAR(36),
            metric        VARCHAR(30)   NOT NULL,
            value         NUMERIC(12,4) NOT NULL,
            unit          VARCHAR(10),
            source        VARCHAR(20)   NOT NULL,
            observed_at   TIMESTAMPTZ   NOT NULL,
            quality       VARCHAR(10)   NOT NULL,
            created_at    TIMESTAMPTZ   DEFAULT now(),
            PRIMARY KEY (id, observed_at)
        )
    """)
    op.execute(
        "SELECT create_hypertable('env_reading', 'observed_at', "
        "chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("CREATE INDEX ON env_reading (user_id, metric, observed_at DESC)")
```

三个细节必须照做，否则后面会返工：

第一，主键必须是 `(id, observed_at)` 复合主键。TimescaleDB 要求分区列进入主键，单列 `id` 主键在超表上会直接报错。这也是全局约定里"时序表除外，用 BIGSERIAL 配合时间分区"的落地方式。

第二，`chunk_time_interval` 取 7 天。按日增 1.44 亿点位估算，7 天一个 chunk 约 10 亿行，落在单 chunk 合理区间；取 1 天会产生过多 chunk，取 30 天会让单个 chunk 过大影响压缩与查询裁剪。

第三，`quality` 三值（`ok` / `suspect` / `stale`）来自产品说明书 4.4.3 功能点 2：异常数据应被标记并排除在阈值判断之外，而不是直接丢弃或直接使用。本项只建列，判定逻辑在 T5-07。

### 三、知识条目向量列

```python
class KnowledgeItem(Base):
    __tablename__ = "knowledge_item"
    __table_args__ = {"schema": "knowledge"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    region_code: Mapped[str | None] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[dict | None] = mapped_column(JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
```

向量索引在 T2-48 建，不在本项。原因是 HNSW 索引的 `m` 与 `ef_construction` 参数需要按真实数据量调，本阶段表里没有数据，提前建索引只会得到一个可能不合适的参数。本项只保证列类型存在且比较算子可用。

### 四、向量维度必须与模型绑定

`Vector(1024)` 的 1024 必须与 T2-56 选定的 embedding 模型输出维度一致。若换模型，需要：

```sql
ALTER TABLE knowledge.knowledge_item ALTER COLUMN embedding TYPE vector(1536);
-- 已有数据必须重新编码，否则维度不匹配报错
```

因此建议在 `Settings` 里增加 `EMBEDDING_DIM: int = 1024`，并在迁移里以 `f-string` 取值，但 Alembic 迁移要求可复现，所以迁移中写死维度、把 `Settings` 的校验放在 `indexer.py` 启动时做（维度不符直接拒绝启动）。这是对技术方案的一处推断，方案未明确维度管理方式。

### 五、验证脚本

`tests/sql/verify_extensions.sql` 提供三条人工核对语句：

```sql
SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','timescaledb');
SELECT hypertable_name FROM timescaledb_information.hypertables WHERE hypertable_name='env_reading';
SELECT column_name, udt_name FROM information_schema.columns
WHERE table_name='knowledge_item' AND column_name='embedding';
```

第三条应返回 `udt_name = vector`。

### 六、与验收项的对应

本项不直接对应产品说明书 9.2 的任何一条，但它是验收项 7（历史列表性能）之外的隐性前置：知识条目索引用 HNSW 还是 IVFFlat 会直接影响 T2-49 检索延迟，而检索延迟又决定问诊首字响应。同时 `env_reading` 在第一步就建好，是为避免产品说明书 9.1 提到的"后期改时序表结构的代价很高"。

## 验收标准

- `alembic upgrade head` 返回 0，且 `SELECT extname FROM pg_extension WHERE extname IN ('vector','timescaledb')` 返回两行。
- `SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name='env_reading'` 返回 1。
- `SELECT column_name, udt_name FROM information_schema.columns WHERE table_name='knowledge_item' AND column_name='embedding'` 返回 `embedding | vector`。
- `INSERT INTO env_reading (user_id, metric, value, source, observed_at, quality) VALUES ('u1','soil_temp',22.5,'sensor', now(),'ok')` 成功，且 `SELECT count(*) FROM env_reading` 为 1。
- 对 `env_reading` 插入一条 `observed_at` 为 30 天前的数据后，`SELECT * FROM timescaledb_information.chunks WHERE hypertable_name='env_reading'` 返回超过 4 个 chunk，证明按 7 天分区生效。
- `alembic downgrade -1` 后 `env_reading` 表消失，但 `SELECT extname FROM pg_extension WHERE extname='vector'` 仍返回一行（证明扩展不随迁移回滚）。
- `pytest backend/tests/test_extensions.py` 全部通过。
- `\d+ env_reading` 中主键列为 `id, observed_at` 两列。
