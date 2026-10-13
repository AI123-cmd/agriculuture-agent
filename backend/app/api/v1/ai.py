"""AI 对话路由：POST /api/ai/chat、GET /api/ai/history、GET /api/ai/consult/{id}、
DELETE /api/ai/history/{id}。

`/ai/chat` 是 T2-33 的图驱动接口：入参为 `ChatRequest`，出参为 `ChatResponse`。
转人工是正常业务结果（用 200 + `handoff` 字段表达），因此本路由不设 4xx/5xx 分支。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas.consult import ChatRequest
from app.services import get_ai_service

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat")
async def chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    response = await get_ai_service().chat(db, user_id, req)
    return ok(response.model_dump(mode="json"))


@router.get("/history")
async def history(
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    return ok(await get_ai_service().history(db, user_id, page, size))


@router.get("/consult/{session_id}")
async def consult_detail(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """回看单个会话的完整消息（历史列表不返回消息）。"""
    return ok(await get_ai_service().conversation_detail(db, user_id, session_id))


@router.delete("/history/{session_id}")
async def delete_history(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    await get_ai_service().delete_history(db, user_id, session_id)
    return ok()
