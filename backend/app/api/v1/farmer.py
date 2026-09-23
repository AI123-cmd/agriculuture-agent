"""农户档案路由：GET/POST /api/farmer/profile。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas import ProfileRequest
from app.services import get_farmer_service

router = APIRouter(prefix="/farmer", tags=["farmer"])


@router.get("/profile")
async def get_profile(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    return ok(await get_farmer_service().get_profile(db, user_id))


@router.post("/profile")
async def save_profile(
    req: ProfileRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    return ok(await get_farmer_service().save_profile(db, user_id, req))
