"""环境监测路由：GET /api/environment/summary、GET /api/environment/detail。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.services import get_environment_service

router = APIRouter(prefix="/environment", tags=["environment"])


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    return ok(await get_environment_service().summary(db, user_id))


@router.get("/detail")
async def detail(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    return ok(await get_environment_service().detail(db, user_id))
