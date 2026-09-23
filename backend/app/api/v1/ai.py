"""AI 对话路由：POST /api/ai/chat、GET /api/ai/history、DELETE /api/ai/history/{id}。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas import ChatRequest
from app.services import get_ai_service

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat")
async def chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    data = await get_ai_service().chat(db, user_id, req)
    return ok(data)


@router.get("/history")
async def history(
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    return ok(await get_ai_service().history(db, user_id, page, size))


@router.delete("/history/{session_id}")
async def delete_history(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    await get_ai_service().delete_history(db, user_id, session_id)
    return ok()
