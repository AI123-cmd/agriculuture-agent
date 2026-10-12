"""设备上报路由：POST /api/device/report/{device_sn}（T5-06，技术方案 13.3）。

**不属于小程序域**：设备侧没有微信登录态，本接口不走 `get_current_user`，改用绑定下发的设备
密钥（`X-Device-Secret` 请求头）鉴权。因此它也不出现在聚合路由的令牌依赖里。

**对设备宽容，对安全严格**：报文收原始文本（`Body`）而非 `dict`，使"报文不是合法 JSON"能返回
200（`payload_unparsable`）而不是被 FastAPI 的 schema 校验打成 422——多数 4G DTU 对 4xx/5xx 都
会重试，把一次格式错误放大成持续压力。唯一的非 200 是密钥/设备校验失败的 403（固件通常不对
403 重试）。

**响应不套 `{code,message,data}`**：调用方是设备固件而非小程序，简洁的 `{accepted,flagged,note}`
比统一包装更省流量，也更贴近技术方案 13.3 的骨架约定。
"""

import logging

from fastapi import APIRouter, Body, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.env_service import ingest_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/device", tags=["device-report"])


@router.post("/report/{device_sn}")
async def device_report(
    device_sn: str,
    raw_payload: str = Body(..., media_type="text/plain"),
    x_device_secret: str = Header(..., alias="X-Device-Secret"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """设备 HTTP 直报入口：鉴权 → 解析 → 标记 → 落库。

    返回 `{"accepted": int, "flagged": int, "note": str | None}`：`accepted` 是 `ok` 读数条数，
    `flagged` 是被标 `suspect`/`stale` 的条数（同样已落库）。失败路径用 `note` 区分原因。
    """
    return await ingest_report(db, device_sn, x_device_secret, raw_payload)
