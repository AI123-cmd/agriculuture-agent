"""Checkpointer 单例：PostgreSQL 短期记忆/断点持久化。"""

from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
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
        _pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
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
    """关闭连接池并释放单例，供应用生命周期收尾与测试重建使用。"""
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _saver, _pool = None, None
