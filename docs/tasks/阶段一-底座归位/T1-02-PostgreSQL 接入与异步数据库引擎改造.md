# T1-02 PostgreSQL 接入与异步数据库引擎改造

## 这一项要做什么

把 `app/db/session.py` 从同步 `create_engine` + `sessionmaker` 改为 SQLAlchemy 2.0 的异步引擎与 `async_sessionmaker`，把 `app/db/base.py` 的时间字段统一为带时区，并让 `get_db` 成为可用的异步依赖。同时把 LangGraph 检查点从 `AsyncSqliteSaver` 换成 `AsyncPostgresSaver`。

换库不是"换一根连接串"，而是因为本产品的查询模式要求"向量相似度 + 强元数据过滤"在同一条语句内完成，且问诊过程要同时写检查点与业务表，两者必须同事务。技术方案 4.3 给了三条理由，见下文第一节。

## 前置依赖

- 依赖 T1-01 已提供：`sqlalchemy[asyncio]`、`asyncpg`、`psycopg[binary]`、`langgraph-checkpoint-postgres`，以及两个连接串环境变量。
- 外部环境：PostgreSQL 16+ 实例可连；已创建 `huanong` 数据库与账号；`public` schema 可写。
- 外部环境：Redis 7+ 就绪（本项不直接使用，但 T1-09 与 T1-13 依赖同一进程内的连接初始化顺序）。

## 参考章节

- 技术方案 第4章 4.3 为什么是 PostgreSQL 统一栈（本项的核心依据）
- 技术方案 第2章 2.1 现有技术栈盘点（"ORM / 迁移保持，改为 async 引擎"）
- 技术方案 第7章 7.5 检查点实现（`AsyncPostgresSaver` 与连接池写法）
- 技术方案 第12章 12.8 迁移策略（切换数据库的数据迁移在 T1-14 落地）
- 全局约定 第四节 数据库约定（主键、时区、索引）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 修改 | `backend/app/db/session.py` | 改为 `create_async_engine` + `async_sessionmaker` + 异步 `get_db` |
| 修改 | `backend/app/db/base.py` | `TimestampMixin` 时间列改带时区；新增 `UUIDPrimaryKeyMixin` |
| 修改 | `backend/app/core/config.py` | 新增 `ENV`、`CHECKPOINT_DB_URL`、`CHECKPOINT_SCHEMA`、池大小等键 |
| 修改 | `backend/app/agent/checkpointer.py` | `AsyncSqliteSaver` 换 `AsyncPostgresSaver` + `AsyncConnectionPool` |
| 修改 | `backend/app/main.py` | lifespan 中关闭引擎与连接池 |
| 修改 | `backend/app/api/deps.py` | 依赖改异步会话，`get_current_user` 改异步 |
| 修改 | `backend/app/api/v1/*.py` | `Depends(get_db)` 的使用处改为 `AsyncSession` |
| 修改 | `backend/tests/conftest.py` | 移除 SQLite `create_all` 依赖，改由迁移建库（见 T1-04） |
| 新增 | `backend/tests/test_async_engine.py` | 断言引擎为异步、连接可用、时区生效 |

## 实现要点

### 一、为什么从 MySQL 换 PostgreSQL

技术方案 4.3 的三条理由，逐条落到本项目的真实查询上。

第一条，知识检索的过滤条件是刚需。茶流程只能检索茶树上已登记的药剂，辣椒流程只能检索辣椒登记标签范围，水稻流程要排除抗药性暂停用药名单。如果向量库与业务库分离，过滤要在两个系统间做二次筛选：要么先取大量向量候选再回业务库过滤，浪费召回预算；要么把过滤条件冗余进向量库，产生一致性风险。pgvector 与业务表同库后，一条语句即可完成：

```sql
SELECT id, 1 - (embedding <=> :query_vec) AS score
FROM knowledge_item
WHERE process_id = 'tea'
  AND status = 'approved'
  AND region_code LIKE '42%'
ORDER BY embedding <=> :query_vec
LIMIT 20;
```

这个查询形态在 MySQL 里无法实现——MySQL 没有原生向量类型与相似度算子，也没有 TimescaleDB 式的超表。T2-49 的混合检索直接依赖这一点。

第二条，检查点与业务状态需要事务一致性。问诊一次请求会同时写 LangGraph 检查点、`consultation_message`、`prescription_card`、`followup_task`。如果检查点在 SQLite、业务表在 MySQL，中断恢复时会出现"检查点说已开方、业务表没有处方卡"。同库同事务可以消除：PostgreSQL 检查点表就建在同一实例的 `checkpoint` schema 下，业务事务与检查点写入可以在一个连接内串行化。

第三条，时序数据量在本产品规模下不需要专用 TSDB。按 100 万农户、每户 1 台设备、10 分钟一次上报估算，日增约 1.44 亿点位。这个量级用 TimescaleDB 的自动分区与压缩已经足够，且它本身就是 PG 扩展，不引入额外运维组件。产品说明书 4.4 明确 P0 传感器只覆盖 5 个指标，不存在高基数爆炸风险。

代价要写清楚：PostgreSQL 替换 MySQL 涉及全部模型与迁移重写，是本次改造中工程量较大且不可回避的一项。但产品说明书 9.4 已列出"迁移改用真实迁移链路，现状首次迁移实际依赖启动建表，需重建"，迁移链路本来就要重做，因此合并进行不构成额外浪费。

### 二、session.py 改写骨架

```python
"""数据库引擎与会话管理（异步）。"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,                 # postgresql+asyncpg://...
    pool_size=settings.DB_POOL_SIZE,       # 默认 10
    max_overflow=settings.DB_MAX_OVERFLOW, # 默认 20
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,   # 关键：提交后仍可读属性，避免路由层二次查询
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话，异常回滚由 T1-09 统一封装。"""
    async with SessionLocal() as session:
        yield session
```

`expire_on_commit=False` 是必需的：现有代码在 `db.commit()` 之后还要读 `user.id`（见 `auth_service.login`），默认 `True` 会触发一次额外的 `SELECT`，在异步上下文里表现为"提交后属性访问悄悄发查询"，既慢又难排查。

### 三、base.py 的时区改造

现有 `TimestampMixin` 用裸 `DateTime`，MySQL 下没有时区概念，迁移到 PG 后会得到 `timestamp without time zone`，与全局约定"时间统一带时区"冲突。改为：

```python
class TimestampMixin:
    create_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
```

同时新增主键混入（全局约定要求业务表主键为 `String(36)` UUID）：

```python
class UUIDPrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)

    # 兼容旧代码的 new_id：保留 new_id(prefix) 但改为生成 36 位以内的稳定长度
```

现有 `new_id` 生成 `user_<24位hex>` 共 29 字符，落在 `String(32)` 里。切换为 `String(36)` 后 `new_id` 可保留原格式（29 字符仍在 36 内），不需要改所有调用点；但新表必须用 `String(36)`，由 T1-04 的基线迁移统一。

### 四、checkpointer.py 改写骨架

```python
"""Checkpointer 单例：PostgreSQL 短期记忆/断点持久化。"""
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings

_saver: AsyncPostgresSaver | None = None
_pool: AsyncConnectionPool | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    global _saver, _pool
    if _saver is None:
        settings = get_settings()
        _pool = AsyncConnectionPool(
            settings.CHECKPOINT_DB_URL,
            min_size=2,
            max_size=settings.CHECKPOINT_POOL_MAX,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "options": f"-c search_path={settings.CHECKPOINT_SCHEMA},public",
            },
            open=False,
        )
        await _pool.open()
        _saver = AsyncPostgresSaver(_pool)
        await _saver.setup()
    return _saver


async def close_checkpointer() -> None:
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _saver, _pool = None, None
```

必须删掉现有 `_saver_loop` 的事件循环判断——那是 SQLite 单连接绑定事件循环的历史包袱（`checkpointer.py` 第 11、27 行）。连接池天然支持跨循环、多 worker，判断逻辑保留反而会在测试隔离时误重建。

`search_path` 写进连接的 `options`，让检查点表落在独立 `checkpoint` schema，与业务表隔离但同库同事务，即技术方案 7.5 的"独立 schema `checkpoint`"。

### 五、main.py 生命周期

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_startup_checks()          # T1-08
    await get_agent()                   # 预热图与检查点
    yield
    await close_checkpointer()
    await engine.dispose()
```

### 六、与验收项的对应

- 本项不单独对应产品说明书 9.2 某一条，但它是验收项 7（历史列表性能）与验收项 8（会话回滚）的地基：异步引擎是 `T1-09` 事务边界与 `T1-11` 批量查询的前提。
- 检查点同库同事务是技术方案 12.3 之后的"归属安全"在中断恢复路径上不出现越权的必要条件。

## 验收标准

- `pytest backend/tests/test_async_engine.py` 全部通过，断言 `engine.dialect.name == "postgresql"` 且 `engine.dialect.driver == "asyncpg"`。
- 在配置了真实 PG 的环境下，`python -c "import asyncio; from app.db.session import engine; asyncio.run(engine.connect().__aenter__())"` 可正常建立连接（无 `pymysql` 相关错误）。
- `python -c "from app.agent.checkpointer import get_checkpointer; import asyncio; asyncio.run(get_checkpointer())"` 返回 `AsyncPostgresSaver`，且数据库中 `SELECT count(*) FROM information_schema.tables WHERE table_schema='checkpoint'` 大于 0。
- 检查点表落在 `checkpoint` schema：`SELECT table_schema, table_name FROM information_schema.tables WHERE table_name LIKE 'checkpoint%'` 的 `table_schema` 均为 `checkpoint`。
- `SELECT column_name, data_type FROM information_schema.columns WHERE table_name='user' AND column_name IN ('create_time','update_time')` 的 `data_type` 为 `timestamp with time zone`。
- 连续两次调用 `get_checkpointer()` 返回同一对象（单例），且 `close_checkpointer()` 后再调用可重新建立。
- `grep -rn "create_engine\|AsyncSqliteSaver\|aiosqlite" backend/app` 无输出。
- `ruff check backend/app` 与 `mypy backend/app` 通过。
