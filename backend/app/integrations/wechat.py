"""微信 jscode2session 集成。"""
import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException


def code2session(code: str) -> dict:
    """用 code 换取 openid / session_key / unionid。

    配置了 WECHAT_APP_SECRET 时调用真实微信接口；否则降级为本地 mock，
    保证本地开发可用。
    """
    settings = get_settings()
    if not settings.WECHAT_APP_SECRET:
        return {"openid": "mock_openid_" + code[:8], "mock": True}

    url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {
        "appid": settings.WECHAT_APP_ID,
        "secret": settings.WECHAT_APP_SECRET,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    try:
        resp = httpx.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise AppException(500, f"微信登录服务异常：{e}") from e

    if data.get("errcode"):
        raise AppException(401, f"微信登录失败：{data.get('errmsg', 'code无效')}")
    return data