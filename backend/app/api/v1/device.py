"""设备接入路由：POST /api/device/bind、POST /api/device/parse-sample（T5-03 / T5-04，技术方案 13.4）。

挂在 `/device` 前缀下，聚合前缀由 `app/main.py` 给出为 `/api`（与其余接口一致，无 `v1` 段）。
归属校验走 `get_current_user`：接口属于小程序域，令牌无效即 401；`user_id` 只从令牌取，
不接受请求体传入（产品说明书 4.5.2 功能点 4 的数据隔离）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas.device import BindRequest, BindResponse, ParseSampleRequest, ParseSampleResponse
from app.services.device_service import bind_device, parse_sample

router = APIRouter(prefix="/device", tags=["device"])


@router.post("/bind")
async def bind(
    req: BindRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """绑定设备编号并下发设备密钥（三步接入第一步）。"""
    result: BindResponse = await bind_device(db, user_id, req)
    return ok(result.model_dump(mode="json", by_alias=True))


@router.post("/parse-sample")
async def parse_sample_route(
    req: ParseSampleRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """解析样例报文并推断字段含义（三步接入第二步）。"""
    result: ParseSampleResponse = await parse_sample(db, user_id, req)
    return ok(result.model_dump(mode="json", by_alias=True))
