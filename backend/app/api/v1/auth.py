"""认证路由：POST /api/login。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import ok
from app.db.session import get_db
from app.schemas import LoginRequest
from app.services import get_auth_service

router = APIRouter(tags=["auth"])


@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    data = await get_auth_service().login(db, req.code, req.userInfo)
    return ok(data)
