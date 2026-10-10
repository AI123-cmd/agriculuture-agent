"""微信集成：jscode2session 登录 + 一次性订阅消息发送。

本模块**不再自行降级**为 mock：缺密钥时直接抛 `ConfigError`，把"是否允许模拟登录"
的判断交给 `AuthService.login`，并受 `ENV` 约束。

改动理由：原实现在 `WECHAT_APP_SECRET` 为空时内部返回一个 mock openid，这让
`AuthService` 无从知晓自己拿到的究竟是真实校验结果还是模拟结果——生产环境只要漏配
密钥就会静默地对任意 code 签发正式 token（产品说明书 9.2 第 3 条）。
集成层的职责是"忠实反映外部服务的结果与错误"，策略判断不该藏在这里。

T3-12 在此基础上扩展出一次性订阅消息发送（`send_subscribe_message`）：`access_token`
缓存在 Redis（有效期 7200 秒，留 300 秒刷新余量），错误码映射成**可在调用方分流**的异常：

- `WechatUserRefusedError`（`43101`）：农户在授权后又关闭了订阅。系统持有的授权已经失效，
  调用方应把该户 `remaining` 归零并记 `no_grant`，而不是当作接口错误退还额度——否则下次
  扫描还会拿着同一份失效授权反复失败。
- `WechatApiError`（其余错误码 / 网络异常）：真正的接口失败，调用方退还额度并记 `api_error`。

本模块只负责"如实上报错误码"，策略判断（退还还是清零额度）留在 `app/broadcast/notifier.py`。
"""

import logging

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException, ConfigError
from app.db.redis_client import get_redis

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://api.weixin.qq.com/sns/jscode2session"
_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
_SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"

# access_token 缓存键与有效期（秒）。微信侧有效期 7200 秒，这里留 300 秒刷新余量，
# 避免临界点上拿到一个恰好失效的 token。
_TOKEN_CACHE_KEY = "wechat:access_token"
_TOKEN_TTL = 7200
_TOKEN_REFRESH_MARGIN = 300

# 微信错误码（订阅消息接口）。映射成异常类型由调用方分流，本模块不自行决定退额策略。
ERR_INVALID_OPENID = 40003  # openid 无效
ERR_USER_REFUSED = 43101  # 农户拒绝接收（授权已失效）
ERR_RATE_LIMITED = 45011  # 频率限制


class WechatApiError(AppException):
    """微信接口调用失败（非"授权失效"类）。

    取 502 语义（上游服务异常）：调用方据此**退还额度**并记 `api_error`，下次扫描重试。
    `errcode` 保留微信返回的原始错误码，供排障；`code` 只是 HTTP 语义标注，本异常不直接
    返回给前端（只消费在 worker 的推送链路里）。
    """

    def __init__(self, message: str = "微信接口调用失败", *, errcode: int | None = None):
        super().__init__(502, message)
        self.errcode = errcode


class WechatUserRefusedError(WechatApiError):
    """农户拒绝接收订阅消息（`43101`）：授权已失效，调用方应清零该户额度并记 `no_grant`。

    作为 `WechatApiError` 的子类，调用方若只捕获基类也不会漏掉它；但要区分处理时必须
    先捕获本类（`except WechatUserRefusedError` 在前）。
    """


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
            resp = await client.get(_LOGIN_URL, params=params)
        data = resp.json()
    except Exception as e:  # noqa: BLE001  网络层异常种类繁多，统一转为业务异常
        raise AppException(500, f"微信登录服务异常：{e}") from e

    if data.get("errcode"):
        raise AppException(401, f"微信登录失败：{data.get('errmsg', 'code无效')}")
    return data


async def _fetch_access_token() -> str:
    """向微信换取 `access_token`，失败抛 `WechatApiError`（含配置缺失的 `ConfigError`）。"""
    settings = get_settings()
    if not settings.WECHAT_APP_ID or not settings.WECHAT_APP_SECRET:
        raise ConfigError("微信订阅消息凭证未配置，无法发起发送")

    params = {
        "grant_type": "client_credential",
        "appid": settings.WECHAT_APP_ID,
        "secret": settings.WECHAT_APP_SECRET,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_TOKEN_URL, params=params)
        data = resp.json()
    except Exception as e:  # noqa: BLE001  网络层异常统一转接口失败
        raise WechatApiError(f"微信 access_token 获取异常：{e}") from e

    token = data.get("access_token")
    if not token:
        raise WechatApiError(
            f"微信 access_token 获取失败：{data.get('errmsg', '未知错误')}",
            errcode=data.get("errcode"),
        )
    return str(token)


async def _get_access_token() -> str:
    """取 `access_token`：优先 Redis 缓存，未命中则换取并回填。

    Redis 不可用（未配置或异常）时**不缓存**，每次直接换取——缓存是优化而非正确性前提，
    连不上缓存不能成为不发送的理由。缓存读写异常一律吞掉并退回直取，避免把一次 Redis 抖动
    放大成推送失败。
    """
    client = get_redis()
    if client is not None:
        try:
            cached = await client.get(_TOKEN_CACHE_KEY)
            if cached:
                return str(cached)
        except Exception as e:  # noqa: BLE001  缓存读失败不阻断发送，退回直取
            logger.warning("读取 access_token 缓存失败，改为直取：%s", e)

    token = await _fetch_access_token()
    if client is not None:
        try:
            await client.set(_TOKEN_CACHE_KEY, token, ex=_TOKEN_TTL - _TOKEN_REFRESH_MARGIN)
        except Exception as e:  # noqa: BLE001  缓存写失败不影响本次发送
            logger.warning("写入 access_token 缓存失败，本次不缓存：%s", e)
    return token


async def send_subscribe_message(
    *,
    openid: str,
    data: dict,
    page: str | None = None,
    template_id: str | None = None,
) -> None:
    """发送一条一次性订阅消息。成功返回 `None`，失败抛可分流的具体异常。

    `data` 是模板字段（形如 `{"thing1": {"value": "..."}}`），由调用方从已落库的提示条目
    构造——本函数不接受"整段自由文案"，内容一致性的第一层约束在调用方（`notify_item`），
    第二层由 `reach_record.prompt_item_id` 外键保证（技术方案 11.8）。

    `43101` 映射为 `WechatUserRefusedError`（授权失效），其余错误码与网络异常映射为
    `WechatApiError`。两者都由调用方决定"退还还是清零额度"。
    """
    settings = get_settings()
    tpl = template_id or settings.WECHAT_SUBSCRIBE_TEMPLATE_ID
    if not tpl:
        raise ConfigError("微信订阅消息模板未配置（WECHAT_SUBSCRIBE_TEMPLATE_ID）")

    token = await _get_access_token()
    body: dict = {
        "touser": openid,
        "template_id": tpl,
        "data": data,
        "lang": "zh_CN",
    }
    if page:
        body["page"] = page

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_SEND_URL, params={"access_token": token}, json=body)
        result = resp.json()
    except Exception as e:  # noqa: BLE001  网络层异常统一转接口失败
        raise WechatApiError(f"微信订阅消息发送异常：{e}") from e

    errcode = int(result.get("errcode", 0) or 0)
    if errcode == 0:
        return
    errmsg = result.get("errmsg", "未知错误")
    if errcode == ERR_USER_REFUSED:
        raise WechatUserRefusedError(f"农户拒绝接收订阅消息（{errcode}: {errmsg}）", errcode=errcode)
    raise WechatApiError(f"微信订阅消息发送失败（{errcode}: {errmsg}）", errcode=errcode)


__all__ = [
    "ERR_INVALID_OPENID",
    "ERR_RATE_LIMITED",
    "ERR_USER_REFUSED",
    "WechatApiError",
    "WechatUserRefusedError",
    "code2session",
    "send_subscribe_message",
]
