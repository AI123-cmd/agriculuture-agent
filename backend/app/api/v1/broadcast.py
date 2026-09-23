"""农事播报路由：GET /api/broadcast/today、POST /api/broadcast/mark-done、POST /api/broadcast/preview。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas import MarkDoneRequest
from app.services import get_broadcast_service

router = APIRouter(prefix="/broadcast", tags=["broadcast"])


@router.get("/today")
async def get_today(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    data = await get_broadcast_service().get_today(db, user_id)
    return ok(data)


@router.post("/mark-done")
async def mark_done(
    req: MarkDoneRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    await get_broadcast_service().mark_done(db, user_id, req.itemId)
    return ok()


@router.post("/preview")
async def preview(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    """实时生成今日播报但不落库，便于前端/联调调试。"""
    from app.broadcast import generate_broadcast
    from app.repositories import Repositories

    results = await generate_broadcast(Repositories(db), user_id)
    items = [
        {
            "type": r.type,
            "level": r.level,
            "title": r.title,
            "content": r.content,
            "source": r.source,
            "action": r.action,
        }
        for r in results
    ]
    return ok({"items": items})
