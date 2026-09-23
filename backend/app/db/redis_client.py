"""Redis 连接（可选）。未配置或连接失败时返回 None，由上层降级处理。"""

import redis

from app.core.config import get_settings

_client: redis.Redis | None = None
_initialized = False


def get_redis() -> redis.Redis | None:
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    url = get_settings().REDIS_URL
    if not url:
        return None
    try:
        _client = redis.Redis.from_url(url, decode_responses=True)
        _client.ping()
    except Exception:
        _client = None
    return _client
