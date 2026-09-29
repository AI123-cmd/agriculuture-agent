"""知识条目核准路由（T2-54）：POST /api/knowledge/review/{knowledge_id}。

后台核准接口，与农户侧接口分离。它落实方案 9.2 的"L3 处置知识必须经过核准流程"：条目
必须经人工审核才能从 `draft` 变为 `approved`，暂停/打回/废弃必须留下理由与操作人。

权限：`require_review_operator` 先验令牌（401）再查运营白名单（403），农户令牌无法调用。
响应：非法迁移（409）、缺理由（400）、条目不存在（404）都由异常自带 code 映射，路由不再翻译。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_review_operator
from app.core.response import ok
from app.db.session import get_db
from app.schemas.knowledge_review import KnowledgeReviewRequest
from app.services.knowledge_review import KnowledgeReviewService

router = APIRouter(prefix="/knowledge", tags=["knowledge-review"])


@router.post("/review/{knowledge_id}")
async def review_knowledge(
    knowledge_id: str,
    req: KnowledgeReviewRequest,
    db: AsyncSession = Depends(get_db),
    operator_id: str = Depends(require_review_operator),
) -> dict:
    """对一条知识条目执行状态迁移（提交/核准/打回/暂停/恢复/废弃）。"""
    item = await KnowledgeReviewService(db).apply(
        knowledge_id=knowledge_id,
        action=req.action,
        operator_id=operator_id,
        reason=req.reason,
        consulted_ref=req.consultedRef,
    )
    return ok({"knowledgeId": item.knowledge_id, "status": item.status})
