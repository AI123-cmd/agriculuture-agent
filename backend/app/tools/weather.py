"""天气工具（MCP）：查询当前/预报天气。"""

from app.integrations.weather import get_weather_summary


def query_weather(location: str = "") -> dict:
    """查询天气摘要，底层走 app/integrations/weather.py。"""
    return {
        "location": location,
        "summary": get_weather_summary(location),
        "source": "weather",
    }
