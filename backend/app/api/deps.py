"""FastAPI 依赖注入：从 Authorization header 解析令牌并提取当前用户。"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Unauthorized
from app.db.session import get_db
from app.services import get_auth_service

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """校验 Bearer 令牌，返回当前用户 id；无效则抛 401。

    走 `AuthService.verify` 而不是直接解码：只有这条链路包含吊销名单检查，
    登出才能让旧令牌立即失效（产品说明书 9.2 第 2 条）。
    """
    if cred is None or not cred.credentials:
        raise Unauthorized()
    user = await get_auth_service().verify(db, cred.credentials)
    return user.id


__all__ = ["get_current_user"]
