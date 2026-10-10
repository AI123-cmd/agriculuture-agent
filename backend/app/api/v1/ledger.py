"""处方卡入台账路由：POST /api/ai/prescription/{id}/ledger（T3-15，技术方案 13.1）。

挂在 `/ai` 前缀下（与 `app/api/v1/ai.py` 同前缀、同标签域），路径为
`/ai/prescription/{id}/ledger`。归属校验统一走 `get_current_user`（T1-06 口径）——令牌无效即
401，服务层再以 `user_id` 过滤，读不到他人处方卡即 404（不区分"不存在"与"不属于你"）。
`user_id` 只从令牌取，不接受请求体传入（产品说明书 4.5.2 功能点 4 的数据隔离）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.models import LedgerEntry
from app.schemas.ledger import LedgerSaveResponse
from app.services.ledger_service import save_prescription_to_ledger

router = APIRouter(prefix="/ai", tags=["ledger"])


def _to_response(entry: LedgerEntry) -> LedgerSaveResponse:
    """台账条目 → 入台账回执视图。

    `ledgerSaved=True` 是固定的：本接口无论走新建还是幂等命中，返回时处方卡都已是"已存入台账"
    状态（幂等命中分支此前已回写），前端据此把按钮切到已存入、抑制重复点击。
    """
    return LedgerSaveResponse(
        entryId=entry.id,
        entryDate=entry.entry_date,
        processId=entry.process_id,
        title=entry.title,
        actions=list(entry.actions or []),
        pesticides=list(entry.pesticides or []),
        safetyIntervalDays=entry.safety_interval_days,
        followupNeeded=entry.followup_needed,
        ledgerSaved=True,
    )


@router.post("/prescription/{card_id}/ledger")
async def save_prescription_to_ledger_route(
    card_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """把处方卡存入农事台账（幂等，重复点击返回既有条目）。"""
    entry = await save_prescription_to_ledger(db, user_id, card_id)
    return ok(_to_response(entry).model_dump(mode="json", by_alias=True))
