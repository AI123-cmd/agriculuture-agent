"""认证路由：POST /api/login、POST /api/logout。"""

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas import LoginRequest
from app.services import get_auth_service

router = APIRouter(tags=["auth"])

# 登出需要拿原始令牌去写吊销名单，故这里直接取凭证本体，不复用 get_current_user
_bearer_required = HTTPBearer(auto_error=False)


@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    data = await get_auth_service().login(db, req.code, req.userInfo)
    return ok(data)


@router.post("/logout")
async def logout(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer_required),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """登出。依赖 `get_current_user` 是刻意的：未登录调登出应得 401 而非静默成功。"""
    token = cred.credentials if cred else ""
    await get_auth_service().logout(db, user_id, token)
    return ok()
