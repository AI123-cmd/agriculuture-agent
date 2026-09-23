"""用户路由：POST /api/user/avatar/upload、DELETE /api/user/cancel。"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.services import get_user_service

router = APIRouter(prefix="/user", tags=["user"])


@router.post("/avatar/upload")
async def upload_avatar(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    url = await get_user_service().upload_avatar(db, user_id, file)
    return ok({"url": url})


@router.delete("/cancel")
async def cancel_account(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    await get_user_service().cancel_account(db, user_id)
    return ok()
