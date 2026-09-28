"""Checkpointer 单例：PostgreSQL 短期记忆/断点持久化。"""

from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings

_saver: AsyncPostgresSaver | None = None
# 显式标注连接行类型：kwargs 里传 row_factory=dict_row，mypy 无法从 dict 推断，需在此声明
_pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """返回 PostgreSQL Checkpointer 单例。

    连接池天然支持跨事件循环与多 worker，故不再保留 SQLite 时代的
    `_saver_loop` 判断——那会在测试隔离时误触发重建。
    """
    global _saver, _pool
    if _saver is None:
        settings = get_settings()
        schema = settings.CHECKPOINT_SCHEMA
        _pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
            settings.CHECKPOINT_DB_URL,
            min_size=2,
            max_size=settings.CHECKPOINT_POOL_MAX,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                # search_path 里 schema 若不存在会被静默忽略，表就落到 public，
                # 与"检查点独立 schema"的要求不符，故下面显式先建 schema。
                "options": f"-c search_path={schema},public",
            },
            open=False,
        )
        await _pool.open()
        # AsyncPostgresSaver.setup() 只建表不建 schema，schema 必须由调用方保证存在
        async with _pool.connection() as conn:
            await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        _saver = AsyncPostgresSaver(_pool)
        await _saver.setup()
    return _saver


async def close_checkpointer() -> None:
    """关闭连接池并释放单例，供应用生命周期收尾与测试重建使用。

    同时重置已编译的图单例：图在编译时**绑定**当前 saver，若只释放 saver 而保留图，
    下次 `get_agent()` 会返回仍指向已关闭连接池的旧图，请求随即抛 `PoolClosed`。
    这里用局部导入避免与 `app.agent.__init__` 形成模块级循环依赖。
    """
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _saver, _pool = None, None
    from app.agent import reset_agent

    reset_agent()
