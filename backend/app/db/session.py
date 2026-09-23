"""数据库引擎与会话管理（异步）。"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,  # postgresql+asyncpg://...
    pool_size=settings.DB_POOL_SIZE,  # 默认 10
    max_overflow=settings.DB_MAX_OVERFLOW,  # 默认 20
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 关键：提交后仍可读属性，避免路由层二次查询
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话，异常回滚由 T1-09 统一封装。"""
    async with SessionLocal() as session:
        yield session
