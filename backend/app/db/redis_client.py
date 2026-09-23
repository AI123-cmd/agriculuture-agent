"""Redis 连接（异步）。未配置时返回 None，由上层决定如何降级。

用 `redis.asyncio` 而非同步客户端：吊销检查在 `AuthService.verify` 里，处在
请求链路的关键路径上，同步客户端会阻塞事件循环。该客户端同时服务 T1-13 的
发件箱投递与后续的额度幂等，是本阶段共用的底座。
"""

import logging

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None
_initialized = False
# 连通性探测结果：None 表示尚未探测。探测只做一次，避免每个请求都 ping 一次。
_usable: bool | None = None


def get_redis() -> aioredis.Redis | None:
    """返回异步客户端；`REDIS_URL` 为空时返回 None（上层据此退化）。

    这里不做网络连接，也就不做"可用性"判断——`from_url` 只构造对象。
    生产环境的连通性问题由启动自检负责拦截（T1-08）。
    """
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    url = get_settings().REDIS_URL
    if not url:
        return None
    _client = aioredis.Redis.from_url(url, decode_responses=True)
    return _client


async def usable_redis() -> aioredis.Redis | None:
    """带连通性探测的取用入口：连不上返回 None，由调用方退化处理。

    与 `get_redis()` 并存是刻意的：启动自检要拿到客户端并自己 ping（好把失败
    原因原样抛给运维），而请求链路要的是"能连就用、连不上别把请求打挂"。两者的
    需求不同，故不合并。探测结果缓存，只 ping 一次。
    """
    global _usable
    client = get_redis()
    if client is None:
        return None
    if _usable is None:
        try:
            await client.ping()
            _usable = True
        except Exception as e:  # noqa: BLE001  探测失败原因不决定分支，一律视为不可用
            logger.warning("Redis 不可用，相关功能退化为进程内实现：%s", e)
            _usable = False
    return client if _usable else None


def reset_redis() -> None:
    """清空连接与探测缓存，供测试在改动配置后重新初始化。"""
    global _client, _initialized, _usable
    _client = None
    _initialized = False
    _usable = None


__all__ = ["get_redis", "reset_redis", "usable_redis"]
