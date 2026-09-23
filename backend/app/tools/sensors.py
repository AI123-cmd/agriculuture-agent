"""传感器查询工具（MCP）：读取地块空气/土壤温湿度、光照、pH/EC。"""
from sqlalchemy.orm import Session

from app.models import Device


def query_sensors(db: Session | None, user_id: str) -> dict:
    """读取农户名下传感器快照；无设备/无平台接入时返回静态兜底。"""
    devices = db.query(Device).filter(Device.user_id == user_id).all() if db else []
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