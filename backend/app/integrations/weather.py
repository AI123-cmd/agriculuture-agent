"""天气数据集成，无配置时返回静态兜底。"""
import httpx

from app.core.config import get_settings


def get_weather_summary(location_name: str = "") -> str:
    settings = get_settings()
    if not settings.WEATHER_API_URL:
        return "多云 26°C"

    try:
        resp = httpx.get(
            settings.WEATHER_API_URL,
            params={"key": settings.WEATHER_API_KEY, "location": location_name},
            timeout=10,
        )
        data = resp.json()
        # 字段按实际天气厂商返回结构调整，这里做最小适配
        cond = data.get("condition") or data.get("text") or "多云"
        temp = data.get("temperature") or data.get("temp") or "26"
        return f"{cond} {temp}°C"
    except Exception:  # noqa: BLE001
        return "多云 26°C"