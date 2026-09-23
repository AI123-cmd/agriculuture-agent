"""环境监测服务：数据源统一走 tools.sensors，无设备时自动降级为静态兜底。"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Device
from app.tools import sensors


class EnvironmentService:
    def summary(self, db: Session, user_id: str) -> dict:
        s = self._sensor(db, user_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return {
            "temperature": s["air_temperature"],
            "humidity": s["air_humidity"],
            "soilStatus": self._soil_status(s["soil_moisture"]),
            "soilDetail": (
                f"土壤温度{s['soil_temperature']}℃，含水率{s['soil_moisture']}%，pH {s['soil_ph']}"
            ),
            "lightStatus": "良好" if s["light_lux"] >= 30000 else "一般",
            "lightDetail": f"光照强度{s['light_lux']} lux",
            "lastUpdateAt": now,
        }

    def detail(self, db: Session, user_id: str) -> dict:
        s = self._sensor(db, user_id)
        devices = db.query(Device).filter(Device.user_id == user_id).all()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return {
            "metrics": [
                {"id": "temp", "label": "气温", "value": str(s["air_temperature"]), "unit": "°C"},
                {"id": "humidity", "label": "湿度", "value": str(s["air_humidity"]), "unit": "%"},
                {"id": "soil", "label": "土壤湿度", "value": str(s["soil_moisture"]), "unit": "%"},
                {"id": "light", "label": "光照", "value": f"{s['light_lux'] / 1000:.0f}k", "unit": "lux"},
            ],
            "airData": [
                {"label": "空气温度", "value": f"{s['air_temperature']}°C"},
                {"label": "空气湿度", "value": f"{s['air_humidity']}%"},
                {"label": "风速", "value": "2.3 m/s"},
                {"label": "风向", "value": "东南风"},
            ],
            "soilData": [
                {"label": "土壤温度", "value": f"{s['soil_temperature']}°C"},
                {"label": "土壤含水率", "value": f"{s['soil_moisture']}%"},
                {"label": "土壤pH值", "value": f"{s['soil_ph']}"},
                {"label": "电导率(EC)", "value": f"{s['soil_ec']} mS/cm"},
            ],
            "weatherData": [
                {"label": "光照强度", "value": f"{s['light_lux']} lux"},
                {"label": "日照时长", "value": "7.2 h"},
                {"label": "紫外线指数", "value": "中等 (UV 4)"},
                {"label": "降雨量", "value": "0 mm"},
            ],
            "devices": [
                {
                    "id": d.id,
                    "name": d.device_name or d.device_sn,
                    "type": d.device_type,
                    "status": d.status,
                    "lastOnline": self._last_online(d),
                }
                for d in devices
            ],
            "lastUpdateAt": now,
        }

    def _sensor(self, db: Session, user_id: str) -> dict:
        return sensors.query_sensors(db, user_id)

    def _soil_status(self, moisture) -> str:
        if moisture is None:
            return "未知"
        if moisture < get_settings().BROADCAST_SOIL_DRY:
            return "偏干"
        return "适宜"

    def _last_online(self, device: Device) -> str:
        if device.status == 0:
            return "离线"
        return "刚刚"