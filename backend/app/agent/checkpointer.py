"""Checkpointer 单例：SQLite 短期记忆/断点持久化（异步）。"""
import asyncio

import aiosqlite

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import get_settings

_saver: AsyncSqliteSaver | None = None
_saver_loop: asyncio.AbstractEventLoop | None = None


def _db_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return url


async def get_checkpointer() -> AsyncSqliteSaver:
    """返回 SQLite Checkpointer 单例，长期持有连接以持久化断点。

    AsyncSqliteSaver 绑定创建时的事件循环；跨循环（如测试隔离）自动重建。
    """
    global _saver, _saver_loop
    loop = asyncio.get_running_loop()
    if _saver is None or _saver_loop is not loop:
        conn = aiosqlite.connect(_db_path(get_settings().CHECKPOINT_DB_URL))
        _saver = AsyncSqliteSaver(conn)
        _saver_loop = loop
    return _saver