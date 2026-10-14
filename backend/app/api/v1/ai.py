"""AI 对话路由：POST /api/ai/chat、POST /api/ai/chat/upload、GET /api/ai/history、
GET /api/ai/consult/{id}、DELETE /api/ai/history/{id}。

`/ai/chat` 是 T2-33 的图驱动接口：入参为 `ChatRequest`，出参为 `ChatResponse`。
转人工是正常业务结果（用 200 + `handoff` 字段表达），因此本路由不设 4xx/5xx 分支。
"""

import logging

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.exceptions import BadRequest
from app.core.response import ok
from app.core.validators import validate_image
from app.db.session import get_db
from app.integrations.storage import get_storage
from app.schemas.consult import ChatRequest
from app.services import get_ai_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat")
async def chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    response = await get_ai_service().chat(db, user_id, req)
    return ok(response.model_dump(mode="json"))


@router.post("/chat/upload")
async def upload_chat_image(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
) -> dict:
    """问诊图片上传：校验后落盘，返回可访问地址（技术方案 13.1）。

    校验顺序与头像上传一致：大小粗筛 → `validate_image` 三重校验 → 才落盘。问诊图片
    **不落库**（地址随 `ChatRequest.imageUrl` 走，由 `AIMessage.image_url` 在落消息时
    一并保存），故本接口不需要 `db` 依赖也不需要 `unit_of_work`。
    """
    settings = get_settings()
    if file.size and file.size > settings.UPLOAD_MAX_BYTES:
        raise BadRequest(f"文件超过上限 {settings.UPLOAD_MAX_BYTES // 1024 // 1024}MB")
    data = await file.read()
    check = validate_image(
        file.filename or "",
        file.content_type,
        data,
        max_bytes=settings.UPLOAD_MAX_BYTES,
        allowed=settings.UPLOAD_ALLOWED_TYPES,
    )
    if not check.ok or check.mime is None:
        logger.warning("问诊图片被拒：user=%s file=%s reason=%s", user_id, file.filename, check.reason)
        raise BadRequest(check.reason or "图片校验失败")
    return ok({"url": get_storage().save_image(data, check.mime)})


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
