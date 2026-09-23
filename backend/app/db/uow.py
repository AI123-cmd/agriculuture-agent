"""显式工作单元：把事务边界从仓储内部上移到业务用例。

仓储只负责构造语句，提交与回滚在这里统一决定，好处是"一次业务用例要么全成功、
要么全回滚"成为可以肉眼检查的事实，而不是散落在各服务方法里的 `commit()` 调用。

与任务骨架的一处偏差（必要）：骨架用 `db.in_transaction()` 判断"是否已有外层事务"，
但 SQLAlchemy 2.0 的会话是 autobegin 的——任何一次 SELECT 都会开启事务。于是
`ai_service.chat` 先做 `resolve_session_id` 查询、再跑图、最后才进 `unit_of_work`，
此时 `in_transaction()` 已为真，会被误判成"嵌套在内层"，只发 SAVEPOINT 而不提交；
外层那个由查询隐式开启的事务最终被 `get_db` 的兜底分支回滚，**写入会静默丢失**。

改为用会话上的显式深度标记判断嵌套：只有"uow 套 uow"才算嵌套，最外层 uow 负责提交。
这与骨架的设计意图一致（内层失败只回滚内层、外层可继续），同时消除了 autobegin 带来的歧义。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ConflictError, TransactionError

# 会话级嵌套深度，存在 Session.info 上（SQLAlchemy 2.0 提供的会话字典）
_DEPTH_KEY = "uow_depth"


@asynccontextmanager
async def unit_of_work(db: AsyncSession, *, name: str) -> AsyncIterator[AsyncSession]:
    """一次业务用例一个事务。内层不调用 `db.commit()`，由本上下文统一提交或回滚。

    用法：`async with unit_of_work(db, name="ai.chat"): ...仓储读写...`

    `name` 必填：用于日志与 trace，使"哪次业务操作回滚了"可检索。
    """
    depth = db.info.get(_DEPTH_KEY, 0)
    if depth > 0:
        # 已有外层 uow：嵌套进 SAVEPOINT，失败只回滚内层，外层可决定继续或放弃
        db.info[_DEPTH_KEY] = depth + 1
        try:
            async with db.begin_nested():
                yield db
        finally:
            db.info[_DEPTH_KEY] = depth
        return

    db.info[_DEPTH_KEY] = 1
    try:
        yield db
        await db.commit()
    except IntegrityError as exc:
        # 唯一约束冲突等：这是"重试即可"的并发结果，不是服务端故障，故 409 而非 500
        await db.rollback()
        raise ConflictError() from exc
    except AppException:
        # 业务异常（403/404 等）自带语义化的 code，必须原样上抛：
        # 若统一包装成 TransactionError（500），归属校验的 403 就会变成 500
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise TransactionError(f"{name} 事务失败，已回滚") from exc
    finally:
        db.info[_DEPTH_KEY] = 0


__all__ = ["unit_of_work"]
