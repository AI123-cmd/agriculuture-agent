"""T1-02 异步引擎与检查点：断言引擎为异步、连接可用、时区生效。

这些用例需要真实 PostgreSQL。测试库不可用时由 `requires_postgres`
前置 fixture 跳过，而不是失败——避免把"环境未就绪"误报成"改造出错"。
"""

import inspect

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.checkpointer import close_checkpointer, get_checkpointer
from app.db.session import SessionLocal, engine, get_db


def test_engine_is_async_with_asyncpg_driver() -> None:
    """引擎必须是 asyncpg 驱动的 PostgreSQL 引擎。"""
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"
    assert engine.dialect.is_async is True


def test_session_local_uses_async_session_with_expire_on_commit_false() -> None:
    """会话工厂产出 AsyncSession，且提交后不过期。"""
    assert isinstance(SessionLocal, async_sessionmaker)
    assert SessionLocal.class_ is AsyncSession
    assert SessionLocal.kw["expire_on_commit"] is False
    assert SessionLocal.kw["autoflush"] is False


def test_get_db_is_async_generator() -> None:
    """get_db 必须是异步生成器，FastAPI 才能在异步依赖里消费。"""
    assert inspect.isasyncgenfunction(get_db)


def test_no_sync_engine_left_in_app() -> None:
    """应用代码中不得残留同步 create_engine / AsyncSqliteSaver / aiosqlite。"""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    forbidden = ("create_engine(", "AsyncSqliteSaver", "aiosqlite")
    offenders: list[str] = []
    for path in app_dir.rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in content:
                offenders.append(f"{path.relative_to(app_dir)}: {token}")
    assert offenders == [], f"仍存在同步引擎痕迹：{offenders}"


def test_timestamp_columns_are_timezone_aware() -> None:
    """TimestampMixin 生成的时间列必须带时区，与全局约定一致。"""
    from app.db.base import TimestampMixin

    for column_name in ("create_time", "update_time"):
        column = TimestampMixin.__dict__[column_name].column
        assert column.type.timezone is True, f"{column_name} 未声明 timezone=True"
        assert column.server_default is not None, f"{column_name} 缺少 server_default"


def test_uuid_primary_key_mixin_uses_string_36() -> None:
    """主键混入必须是 String(36)。"""
    from app.db.base import UUIDPrimaryKeyMixin

    column = UUIDPrimaryKeyMixin.__dict__["id"].column
    assert column.type.length == 36
    assert column.primary_key is True


def test_engine_can_connect(requires_postgres) -> None:
    """真实建连：不出现 pymysql/驱动缺失一类错误。"""
    import asyncio

    async def _connect() -> str:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return str(result.scalar_one())

    assert asyncio.run(_connect()) == "1"


def test_business_tables_use_timestamp_with_time_zone(requires_postgres, engine) -> None:
    """建表后 user 表的时间列类型为 timestamp with time zone。"""
    import asyncio

    async def _query() -> dict[str, str]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'user' AND column_name IN ('create_time', 'update_time')"
                )
            )
            return {row[0]: row[1] for row in result.fetchall()}

    columns = asyncio.run(_query())
    assert columns.get("create_time") == "timestamp with time zone"
    assert columns.get("update_time") == "timestamp with time zone"


def test_checkpointer_is_singleton_and_rebuildable(requires_postgres) -> None:
    """连续两次取到同一实例；关闭后可重新建立。"""
    import asyncio

    async def _run() -> tuple[bool, bool]:
        first = await get_checkpointer()
        second = await get_checkpointer()
        same = first is second
        await close_checkpointer()
        third = await get_checkpointer()
        rebuilt = third is not first
        await close_checkpointer()
        return same, rebuilt

    same, rebuilt = asyncio.run(_run())
    assert same is True
    assert rebuilt is True


def test_checkpointer_creates_tables_in_its_schema(requires_postgres, engine) -> None:
    """检查点表落在独立 checkpoint schema 下。"""
    import asyncio

    from app.core.config import get_settings

    async def _run() -> int:
        await get_checkpointer()
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :schema"),
                {"schema": get_settings().CHECKPOINT_SCHEMA},
            )
            count = result.scalar_one()
        await close_checkpointer()
        return int(count)

    assert asyncio.run(_run()) > 0
