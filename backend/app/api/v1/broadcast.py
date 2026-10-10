"""提示快照路由：POST /api/broadcast/snapshot、GET /api/broadcast/snapshot。

两个动作用同一路径、不同方法：POST 是"更新提示"（重新生成/幂等命中并记一次点击），GET 是
读当日快照。归属校验统一走 `get_current_user`（T1-06 口径）——令牌无效即 401，服务层再以
`user_id` 过滤，读不到他人快照。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.services import get_broadcast_service

router = APIRouter(prefix="/broadcast", tags=["broadcast"])


@router.post("/snapshot")
async def post_snapshot(
    farmEntryId: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """生成/刷新当日快照（POST 语义）。"""
    result = await get_broadcast_service().generate(db, user_id, farmEntryId)
    return ok(result.model_dump(mode="json", by_alias=True))


@router.get("/snapshot")
async def get_snapshot(
    farmEntryId: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """读当日快照（GET 语义）。"""
    result = await get_broadcast_service().get_today(db, user_id, farmEntryId)
    return ok(result.model_dump(mode="json", by_alias=True))
