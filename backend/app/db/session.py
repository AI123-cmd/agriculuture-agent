"""数据库引擎与会话管理（异步）。"""

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

logger = logging.getLogger(__name__)

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
    """请求级会话：异常回滚、始终关闭。

    业务提交由服务层的 `unit_of_work` 显式完成，这里只做兜底。

    `finally` 分支刻意**不静默提交**，而是回滚并告警：理由是"忘记提交"比"忘记回滚"
    更常见，替服务层提交会把未经验证的写入落库（fail-open）；回滚会让测试立刻暴露
    漏提交（fail-closed）。后来者若改成自动提交，会同时失去这个安全网与它的提示日志。
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            logger.exception("请求级会话异常，已回滚")
            raise
        finally:
            if session.in_transaction():
                await session.rollback()
                logger.warning("会话结束仍处于事务中，已回滚（服务层可能漏了提交）")
