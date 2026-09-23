"""地点路由：GET /api/location/reverse-geocode、POST /api/location/set。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas import SetLocationRequest
from app.services import get_location_service

router = APIRouter(prefix="/location", tags=["location"])


@router.get("/reverse-geocode")
async def reverse_geocode(latitude: float, longitude: float) -> dict:
    return ok(get_location_service().reverse_geocode(latitude, longitude))


@router.post("/set")
async def set_location(
    req: SetLocationRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    return ok(await get_location_service().set_location(db, user_id, req))
