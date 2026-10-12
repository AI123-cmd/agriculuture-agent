"""设备接入路由：绑定、样例解析、字段映射、列表与解绑（T5-03 / T5-04 / T5-05 / T5-09）。

挂在 `/device` 前缀下，聚合前缀由 `app/main.py` 给出为 `/api`（与其余接口一致，无 `v1` 段）。
归属校验走 `get_current_user`：接口属于小程序域，令牌无效即 401；`user_id` 只从令牌取，
不接受请求体传入（产品说明书 4.5.2 功能点 4 的数据隔离）。

管理入口（列表、解绑）与设备上报入口（T5-06 的 `POST /device/report/{sn}`）分属两套凭据：
前者是农户在本小程序里调用的登录态，后者是设备持有的密钥，不混用。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.schemas.device import (
    BindRequest,
    BindResponse,
    DeviceListResponse,
    MappingSaveRequest,
    MappingSaveResponse,
    ParseSampleRequest,
    ParseSampleResponse,
)
from app.services.device_service import bind_device, list_devices, parse_sample, unbind_device
from app.services.mapping_service import save_mappings

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


@router.post("/mapping")
async def save_mapping_route(
    req: MappingSaveRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """保存设备的完整字段映射并返回换算预览（三步接入第三步）。"""
    result: MappingSaveResponse = await save_mappings(db, user_id, req)
    return ok(result.model_dump(mode="json", by_alias=True))


@router.get("/list")
async def list_devices_route(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """设备管理列表：在绑设备的在线状态、最近上报时间与映射条数（T5-09）。"""
    result: DeviceListResponse = await list_devices(db, user_id)
    return ok(result.model_dump(mode="json", by_alias=True))


@router.delete("/{device_id}")
async def unbind_device_route(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user),
) -> dict:
    """解绑设备（软删除，保留历史读数与映射）。归属不符返回 404（T5-09）。"""
    await unbind_device(db, user_id, device_id)
    return ok({"unbound": True})
