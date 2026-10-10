"""订阅授权路由：GET /api/subscription/quota、POST /api/subscription/grant（T3-11）。

两个接口都走 T1-06 的归属校验（`get_current_user`），`user_id` 从令牌取，**不接受请求体传入**
——额度是每户的资产，允许客户端指定 `user_id` 会造成额度篡改（技术方案 11.7 第四条）。

`GET /quota` 供小程序在生成快照后查询额度以决定是否显示授权请求（技术方案 11.6 序列图）；
`POST /grant` 接收前端回传的授权结果，返回最新额度视图，使前端不需要再发一次查询。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas.subscription import GrantRequest
from app.services.quota_service import get_quota, grant

router = APIRouter(prefix="/subscription", tags=["subscription"])


@router.get("/quota")
async def get_subscription_quota(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """读当前用户的推送额度与降级索要判定。"""
    view = await get_quota(db, user_id)
    return ok(view.model_dump(by_alias=True))


@router.post("/grant")
async def grant_subscription(
    req: GrantRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """记录一次授权结果（同意加额度并清拒绝计数 / 拒绝累加计数），返回最新额度视图。"""
    view = await grant(db, user_id, req)
    return ok(view.model_dump(by_alias=True))
