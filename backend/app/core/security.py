"""安全工具：JWT 签名令牌的签发与解码。

令牌从"32 位随机串 + Redis 键"改为签名令牌，原因有三：Redis 丢失不再让全员
掉线（用户可以靠签名自证身份）；`jti` 让"按令牌粒度吊销"成为可能；`exp` 让
吊销名单的 TTL 有依据——名单只需保留到令牌自然过期，条目数因此与"当前活跃
的已吊销令牌数"成正比，而不是随时间无限增长。

吊销名单本身不在这里，见 `app.core.token_revoker`：本模块只负责令牌的密码学
部分，是纯函数，可脱离 Redis 与数据库单测。
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt

from app.core.config import get_settings


def create_token(user_id: str, *, jti: str | None = None) -> str:
    """签发令牌。`jti` 可显式指定，便于测试构造可预测的令牌。"""
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "jti": jti or uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.TOKEN_EXPIRE_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """验签解码。过期抛 `ExpiredSignatureError`，篡改抛 `InvalidTokenError`。

    这两种异常由调用方（`AuthService.verify`）翻译成 401，本模块不吞异常：
    把"密码学判定"与"业务上的响应码"分开，是本模块能保持纯函数的前提。
    """
    settings = get_settings()
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


def remaining_ttl(token: str) -> int:
    """剩余秒数，用于决定吊销名单条目的存活时间。"""
    return max(0, int(decode_token(token)["exp"]) - int(datetime.now(UTC).timestamp()))


__all__ = ["create_token", "decode_token", "remaining_ttl"]
