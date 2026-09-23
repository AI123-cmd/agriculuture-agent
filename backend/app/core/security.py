"""安全工具：token 生成 / 校验（对接 Redis Session，无 Redis 时降级进程内内存）。"""

import time
import uuid

from app.core.config import get_settings
from app.db.redis_client import get_redis

# 进程内兜底：token -> (user_id, expire_at)
_memory: dict[str, tuple[str, float]] = {}


def create_token(user_id: str) -> str:
    token = uuid.uuid4().hex
    expire = get_settings().TOKEN_EXPIRE_SECONDS
    r = get_redis()
    if r is not None:
        r.setex(f"token:{token}", expire, user_id)
    else:
        _memory[token] = (user_id, time.time() + expire)
    return token


def get_user_id(token: str) -> str | None:
    if not token:
        return None
    r = get_redis()
    if r is not None:
        # 同步 redis 客户端（decode_responses=True）返回 str；stub 的联合返回需显式收敛
        cached = r.get(f"token:{token}")
        return cached if isinstance(cached, str) else None
    item = _memory.get(token)
    if item is None:
        return None
    user_id, expire_at = item
    if time.time() > expire_at:
        _memory.pop(token, None)
        return None
    return user_id


def revoke_token(token: str) -> None:
    r = get_redis()
    if r is not None:
        r.delete(f"token:{token}")
    else:
        _memory.pop(token, None)
