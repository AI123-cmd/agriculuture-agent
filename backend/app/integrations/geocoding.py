"""逆地理编码集成（GPS 坐标 -> 省市县），无配置时返回坐标兜底。"""

import httpx

from app.core.config import get_settings


def reverse_geocode(latitude: float, longitude: float) -> dict:
    settings = get_settings()
    if not settings.GEOCODING_API_URL:
        return {
            "province": "",
            "city": "",
            "district": "",
            "fullAddress": f"{latitude:.4f},{longitude:.4f}",
        }

    try:
        resp = httpx.get(
            settings.GEOCODING_API_URL,
            params={
                "key": settings.GEOCODING_API_KEY,
                "latitude": latitude,
                "longitude": longitude,
            },
            timeout=10,
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {
            "province": "",
            "city": "",
            "district": "",
            "fullAddress": f"{latitude:.4f},{longitude:.4f}",
        }

    # 不同厂商字段不同，这里做宽松提取，实际厂商接入时按返回结构适配
    return {
        "province": data.get("province", ""),
        "city": data.get("city", ""),
        "district": data.get("district", ""),
        "fullAddress": data.get("fullAddress") or data.get("address", ""),
    }
