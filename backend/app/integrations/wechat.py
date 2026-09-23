"""微信 jscode2session 集成。

本模块**不再自行降级**为 mock：缺密钥时直接抛 `ConfigError`，把"是否允许模拟登录"
的判断交给 `AuthService.login`，并受 `ENV` 约束。

改动理由：原实现在 `WECHAT_APP_SECRET` 为空时内部返回一个 mock openid，这让
`AuthService` 无从知晓自己拿到的究竟是真实校验结果还是模拟结果——生产环境只要漏配
密钥就会静默地对任意 code 签发正式 token（产品说明书 9.2 第 3 条）。
集成层的职责是"忠实反映外部服务的结果与错误"，策略判断不该藏在这里。
"""

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException, ConfigError

_URL = "https://api.weixin.qq.com/sns/jscode2session"


async def code2session(code: str) -> dict:
    """用 code 换取 openid / session_key / unionid。缺密钥抛 `ConfigError`。"""
    settings = get_settings()
    if not settings.WECHAT_APP_ID or not settings.WECHAT_APP_SECRET:
        raise ConfigError("微信登录凭证未配置，无法完成真实登录校验")

    params = {
        "appid": settings.WECHAT_APP_ID,
        "secret": settings.WECHAT_APP_SECRET,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_URL, params=params)
        data = resp.json()
    except Exception as e:  # noqa: BLE001  网络层异常种类繁多，统一转为业务异常
        raise AppException(500, f"微信登录服务异常：{e}") from e

    if data.get("errcode"):
        raise AppException(401, f"微信登录失败：{data.get('errmsg', 'code无效')}")
    return data


__all__ = ["code2session"]
