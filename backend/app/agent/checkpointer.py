"""Checkpointer 单例：PostgreSQL 短期记忆/断点持久化。"""

import time
from collections.abc import Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings

_saver: AsyncPostgresSaver | None = None
# 显式标注连接行类型：kwargs 里传 row_factory=dict_row，mypy 无法从 dict 推断，需在此声明
_pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] | None = None


class _ObservedPostgresSaver(AsyncPostgresSaver):
    """在最外层写入路径上埋点的检查点器。

    检查点写入发生在图推进的空隙、不在任何 span 树里（14.3 特意点出"LangGraph checkpoint
    是隐式成本，量大时不可忽略"），故只能直接在 `aput` / `aput_writes` 上计时。用子类而非
    "在调用点包一层"是因为这两个方法由 LangGraph 内部调用（`build_graph` 编译期绑定），
    没有可供外部包装的调用点。

    计时只覆盖真正的 DB 写入（`super()` 的 await），不含上游构造开销；埋点异常一律吞掉——
    观测路径不得让问诊失败（与追踪导出失败被丢弃同一原则）。
    """

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        started = time.perf_counter()
        try:
            return await super().aput(config, checkpoint, metadata, new_versions)
        finally:
            _observe(started)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        started = time.perf_counter()
        try:
            await super().aput_writes(config, writes, task_id, task_path)
        finally:
            _observe(started)


def _observe(started: float) -> None:
    """把一次写入耗时写进进程内直方图。埋点本身失败不得影响检查点写入。"""
    from app.observability.metrics import observe_checkpoint_write_ms

    try:
        observe_checkpoint_write_ms((time.perf_counter() - started) * 1000.0)
    except Exception:  # noqa: BLE001  观测路径绝不打断写入
        pass


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
        _saver = _ObservedPostgresSaver(_pool)
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
