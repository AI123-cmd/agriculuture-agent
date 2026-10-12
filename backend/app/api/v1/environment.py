"""环境监测路由：GET /api/environment/board、/summary、/detail（T5-08）。

`/board` 是本阶段新增的看板接口，返回逐指标的数值、单位、来源质量与状态标注，以及环境数据
的来源与新鲜度标注；`/summary` 与 `/detail` 为既有首页接口，返回结构保持不变，内部数据源已
改为环境事实采集并新增 `sourceBadge` 字段供前端渐进采用。

聚合前缀由 `app/main.py` 给出为 `/api`（与其余接口一致，无 `v1` 段）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.services import get_environment_service

router = APIRouter(prefix="/environment", tags=["environment"])


@router.get("/board")
async def board(
    plot_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """环境看板：指标数值 + 来源与新鲜度标注 + 设备列表 + 最近更新时刻。"""
    result = await get_environment_service().board(db, user_id, plot_id)
    return ok(result.model_dump(mode="json", by_alias=True))


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    return ok(await get_environment_service().summary(db, user_id))


@router.get("/detail")
async def detail(db: AsyncSession = Depends(get_db), user_id: str = Depends(get_current_user)) -> dict:
    return ok(await get_environment_service().detail(db, user_id))
