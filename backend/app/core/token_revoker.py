"""令牌吊销名单：Redis 为生产实现，无 Redis 时退化为进程内集合。

为什么按 `jti` 而不是按 `user_id` 吊销：按用户吊销会把该用户所有设备一起踢下线，
而用户只是想"在这台设备退出"。按 `jti` 精确到单个令牌，重新登录签发的新令牌不受
影响，这是 `/api/logout` 能被正常使用的前提。

名单条目带 TTL（等于令牌剩余存活时间）：令牌自然过期后条目失去意义，让 Redis 自动
回收，名单规模不会随时间无限增长。

生产环境 Redis 不可用必须拒绝启动，由 T1-08 的启动自检拦截；本模块不做"环境判断"，
只做降级——把环境策略与存储实现分开，测试才能在不改 `ENV` 的前提下覆盖两条分支。
"""

import time

from app.db.redis_client import usable_redis

_KEY_PREFIX = "revoked:jti"

# jti -> 过期时间戳。进程内兜底仅对单进程有效：多 worker 部署下各进程名单不共享，
# 故生产必须走 Redis，这也是 T1-08 把 Redis 列为生产必需项的原因之一。
_memory: dict[str, float] = {}


def _prune(now: float | None = None) -> None:
    """清理已自然过期的条目，避免进程内兜底字典无上限增长。"""
    moment = now if now is not None else time.time()
    expired = [jti for jti, expire_at in _memory.items() if expire_at <= moment]
    for jti in expired:
        _memory.pop(jti, None)


class TokenRevoker:
    """令牌吊销名单。"""

    async def revoke(self, jti: str, ttl: int) -> None:
        """把 `jti` 写入名单。`ttl` 取令牌剩余存活时间。"""
        client = await usable_redis()
        if client is not None:
            await client.setex(f"{_KEY_PREFIX}:{jti}", max(ttl, 1), "1")
            return
        _memory[jti] = time.time() + max(ttl, 1)

    async def is_revoked(self, jti: str) -> bool:
        """名单命中即为已吊销。"""
        client = await usable_redis()
        if client is not None:
            return await client.exists(f"{_KEY_PREFIX}:{jti}") == 1
        now = time.time()
        _prune(now)
        return jti in _memory

    def reset_memory(self) -> None:
        """清空进程内名单，供测试隔离用例间状态。"""
        _memory.clear()


token_revoker = TokenRevoker()

__all__ = ["TokenRevoker", "token_revoker"]
