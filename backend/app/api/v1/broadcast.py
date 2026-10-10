"""提示快照路由：POST /api/broadcast/snapshot、GET /api/broadcast/snapshot、
POST /api/broadcast/items/{id}/execute。

前两个动作用同一路径、不同方法：POST 是"更新提示"（重新生成/幂等命中并记一次点击），GET 是
读当日快照。`POST /items/{id}/execute`（T3-15）承载执行勾选回执：`{"executed": true}` 勾选、
`{"executed": false}` 取消，是北极星指标"执行率"的唯一写入入口。归属校验统一走
`get_current_user`（T1-06 口径）——令牌无效即 401，服务层再以 `user_id` 过滤，读不到他人条目。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas.ledger import ExecuteRequest, ExecuteResponse
from app.services import get_broadcast_service
from app.services.ledger_service import set_item_executed

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


@router.post("/items/{item_id}/execute")
async def execute_item(
    item_id: str,
    req: ExecuteRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """勾选或取消勾选一条提示条目的执行回执（T3-15）。

    `req.executed=True` 勾选已执行、`False` 取消。被抑制条目与不存在的条目分别由服务层折算为
    400（`InvalidStateError`）与 404（`NotFound`）。返回勾选结果使前端立即刷新按钮状态。
    """
    item = await set_item_executed(db, user_id, item_id, req.executed)
    result = ExecuteResponse(id=item.id, executed=item.executed, executedAt=item.executed_at)
    return ok(result.model_dump(mode="json", by_alias=True))
