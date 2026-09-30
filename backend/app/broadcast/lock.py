"""快照幂等锁与幂等键（T3-08，技术方案 11.4）。

**锁的层次关系（务必先读这段，别把 Redis 锁当成必需）**：唯一的强一致保障来自数据库
唯一约束（T3-01 的 `(user_id, farm_entry_id, snapshot_date, revision)`）。Redis 锁只是
"减少无效工作"——把同一农户同地块的并发请求串起来，避免两个请求同时跑完整条生成链路
（三类输入采集 + 规则匹配 + 可能的模型改写）。因此 Redis 不可用时本模块降级为 no-op，
让数据库唯一约束兜底，而不是让整条链路失败；抢锁重试后仍未拿到也一律放行，理由同上。

三个实现要点：

1. **锁值用随机 token，释放走 Lua 的 compare-and-delete**。若只 `del` 而不比对 token，
   会出现"A 的锁超时后被 B 拿到，A 执行完却把 B 的锁删掉"，互斥因此失效。比对与删除
   必须原子，故用 Lua 而非"先 `get` 再 `del`"。
2. **抢锁失败短暂等待后重试**，而非直接报错。农户连点两下「更新提示」是可预期的并发，
   直接报错会让农户看到失败提示，观感差且无必要。
3. **全程不整体失败**。`get_redis()` 返回 None、抛异常、抢锁异常、释放异常，都只记日志，
   临界区照常执行——失败面收窄到"没有锁"，而正确性仍由数据库约束保证。
"""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, cast

import redis.asyncio as aioredis

from app.db.redis_client import get_redis

logger = logging.getLogger(__name__)

# 抢锁总尝试次数：首次 + 重试 2 次。农户连点带来的并发是短时的，重试足以吸收掉。
_ACQUIRE_ATTEMPTS = 3
# 每次重试前的等待秒数。取 200ms：够让持锁请求跑完短临界区，又不至于让农户觉察到卡顿。
_RETRY_DELAY_SECONDS = 0.2

# compare-and-delete：仅当值仍是自己写入的 token 才删除，否则不动。整体在 Lua 里执行，
# 保证 GET 与 DEL 之间没有其他客户端插入操作的间隙。
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def snapshot_key(user_id: str, plot_id: str | None, today: date) -> str:
    """幂等键：农户 + 地块 + 自然日，粒度与 T3-01 的唯一约束一致。

    `plot_id or 'default'` 处理杂草等不绑定地块的流程（技术方案 12.2）：让这些流程也有
    一把锁可用，而不是退化成无锁。`today` 用 `isoformat()` 而非 `str()`，跨平台表示稳定。
    """
    return f"snapshot:{user_id}:{plot_id or 'default'}:{today.isoformat()}"


def _client_or_none() -> aioredis.Redis | None:
    """取用 Redis 客户端，任何异常都折算成 None（等价于"不可用"）。

    `get_redis()` 本身只构造对象、不建连，正常不抛异常；但配置读取等环节仍可能出错，
    统一兜住以免把"锁不可用"升级成"请求失败"。
    """
    try:
        return get_redis()
    except Exception as exc:  # noqa: BLE001  不可用原因不决定分支，一律视为不可用
        logger.warning("获取 Redis 客户端失败，幂等锁降级为无锁执行：%s", exc)
        return None


async def _acquire(client: aioredis.Redis, key: str, token: str, ttl: int) -> bool:
    """`SET NX EX` 抢锁；失败则在短暂等待后重试，仍失败返回 False（由调用方决定放行）。

    `ttl` 是锁的自动过期秒数，对外语义上是 TTL 而非函数执行超时，故不命名为 `timeout`
    （也就不会触发 ASYNC109 那条"async 函数应配 `asyncio.timeout`"的建议）。返回 True
    表示已持有锁，释放时必须用同一 token 走 compare-and-delete。
    """
    for attempt in range(_ACQUIRE_ATTEMPTS):
        try:
            if await client.set(key, token, nx=True, ex=ttl):
                return True
        except Exception as exc:  # noqa: BLE001  连接类异常一律按"抢不到"处理
            logger.warning("Redis 抢锁异常（key=%s），降级为无锁执行：%s", key, exc)
            return False
        if attempt < _ACQUIRE_ATTEMPTS - 1:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
    return False


@asynccontextmanager
async def redis_lock(key: str, timeout: int = 30) -> AsyncIterator[None]:  # noqa: ASYNC109
    """分布式锁上下文管理器：`async with redis_lock(key, timeout=30): ...`

    `timeout` 是锁的自动过期秒数，不是函数执行超时——它兜住"持锁进程崩溃导致锁永占"的
    情形（故此处不适用 ASYNC109 建议的 `asyncio.timeout`）。30 秒与快照生成预期耗时匹配：
    三类输入采集加上一次可选的模型改写，留足余量又不至于让卡死的锁长期占位。

    Redis 不可用或抢锁失败时 `yield` 空操作，临界区照常执行，靠数据库唯一约束兜底。
    """
    client = _client_or_none()
    if client is None:
        yield
        return

    token = secrets.token_hex(16)
    if not await _acquire(client, key, token, timeout):
        # 重试用尽仍未拿到锁：仍放行，避免把可预期的并发升级成农户可见的失败。
        logger.warning("未取得 Redis 锁（key=%s），降级为无锁执行，由数据库唯一约束兜底", key)
        yield
        return

    try:
        yield
    finally:
        # 释放失败不掩盖临界区异常，也不回滚已完成的写入；锁带 TTL，最坏情况是等它自然过期。
        # `cast` 是因为 redis 的类型标注对 `eval` 返回的是"可等待对象或直值"的联合类型
        # （兼容 pipeline 同步用法），此处必然走异步分支。
        try:
            await cast("Awaitable[Any]", client.eval(_RELEASE_SCRIPT, 1, key, token))
        except Exception as exc:  # noqa: BLE001  释放失败只影响锁的即时回收，不影响业务结果
            logger.warning("释放 Redis 锁失败（key=%s），将由 TTL 自然过期：%s", key, exc)


__all__ = ["redis_lock", "snapshot_key"]
