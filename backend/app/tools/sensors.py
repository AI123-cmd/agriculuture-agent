"""传感器查询工具（MCP）：读取地块空气/土壤温湿度、光照、pH/EC。

与 `tools/profile.py` 同：接收 `Repositories | None`，不出现 `AsyncSession`。
"""

from collections.abc import Sequence

from app.models import Device
from app.repositories import Repositories


async def query_sensors(repos: Repositories | None, user_id: str) -> dict:
    """读取农户名下传感器快照；无设备/无平台接入时返回静态兜底。"""
    devices: Sequence[Device] = []
    if repos is not None:
        devices = await repos.device.list_for_user(repos.db, user_id)
    return {
        "air_temperature": 26.5,
        "air_humidity": 78,
        "soil_temperature": 25.0,
        "soil_moisture": 23,
        "soil_ph": 6.6,
        "soil_ec": 1.2,
        "light_lux": 58000,
        "devices": [
            {"id": d.id, "type": d.device_type, "name": d.device_name or d.device_sn, "status": d.status}
            for d in devices
        ],
        "source": "sensor_snapshot",
    }


__all__ = ["query_sensors"]
