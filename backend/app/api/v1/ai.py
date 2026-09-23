"""AI 对话路由：POST /api/ai/chat、GET /api/ai/history、DELETE /api/ai/history/{id}。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas import ChatRequest
from app.services import get_ai_service

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat")
async def chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    data = await get_ai_service().chat(db, user_id, req)
    return ok(data)


@router.get("/history")
def history(
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    return ok(get_ai_service().history(db, user_id, page, size))


@router.delete("/history/{session_id}")
def delete_history(
    session_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    get_ai_service().delete_history(db, user_id, session_id)
    return ok()