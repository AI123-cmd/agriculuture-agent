"""设备样例报文的短时缓存（T5-05 依赖，T5-04 写入）。

三步接入的第二步（T5-04）解析农户粘贴的样例报文后，把这条报文暂存为设备的"短时状态"；第三步
（T5-05）据此校验农户提交的每个 `raw_field` 确实来自设备的真实报文，防止前端构造任意字段，
让后续 `parse_payload` 在设备报文中根本找不到值。

**为什么是短时状态而不是落库**：样例报文只在"绑定 → 确认映射"这一次配置流程中有用，农户离开
配置页后即无价值。落库会在 `device_config` 上增加一个几乎总是过期的列，还要配套迁移与清理；
短时缓存随 TTL 自然过期，正好匹配它的生命周期。唯一的取舍是进程重启会丢缓存——此时校验退化为
"仅做非空/长度/枚举"，不会误拒合法映射。

**存储降级与 `app/core/token_revoker.py` 同构**：Redis 可用时用 Redis（多 worker 部署下各进程
共享），不可用时落进程内字典并做 TTL 清理。测试环境 `REDIS_URL` 为空，走内存分支。
"""

import json
import logging
import time
from typing import Any

from app.db.redis_client import usable_redis

logger = logging.getLogger(__name__)

# 样例报文的存活时间：足够覆盖"绑定后慢慢确认映射"的一次配置会话，又不会长期占用存储。
SAMPLE_TTL_SECONDS = 1800

_KEY_PREFIX = "device:sample"

# 进程内兜底：(user_id, device_id) -> (过期时间戳, 报文 JSON 串)
_memory: dict[tuple[str, str], tuple[float, str]] = {}


def _key(user_id: str, device_id: str) -> str:
    return f"{_KEY_PREFIX}:{user_id}:{device_id}"


def _prune(now: float | None = None) -> None:
    """清理已过期的进程内条目，避免字典无上限增长。"""
    moment = now if now is not None else time.time()
    for key, (expire_at, _) in list(_memory.items()):
        if expire_at <= moment:
            _memory.pop(key, None)


async def remember_sample(user_id: str, device_id: str, payload: dict[str, Any]) -> None:
    """暂存一次成功解析的样例报文（T5-04 成功后调用）。

    暂存失败不得影响解析接口的返回：这只是校验用的旁路状态，丢失的后果是 T5-05 退化为弱校验，
    远轻于让农户看到一个解析失败的报错。
    """
    dump = json.dumps(payload, ensure_ascii=False)
    try:
        client = await usable_redis()
        if client is not None:
            await client.setex(_key(user_id, device_id), SAMPLE_TTL_SECONDS, dump)
            return
    except Exception as exc:  # noqa: BLE001  Redis 抖动不应阻断解析接口
        logger.warning("样例报文写入 Redis 失败，退化为进程内缓存：%s", exc)
    _prune()
    _memory[(user_id, device_id)] = (time.time() + SAMPLE_TTL_SECONDS, dump)


async def last_sample_payload(user_id: str, device_id: str) -> dict[str, Any] | None:
    """取该设备最近一次样例报文；无缓存或已过期返回 `None`。"""
    try:
        client = await usable_redis()
        if client is not None:
            raw = await client.get(_key(user_id, device_id))
            return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001  读不到就当没有缓存，由调用方弱化校验
        logger.warning("样例报文读取 Redis 失败，退化为进程内缓存：%s", exc)

    _prune()
    entry = _memory.get((user_id, device_id))
    return json.loads(entry[1]) if entry is not None else None


def reset_memory() -> None:
    """清空进程内缓存，供测试隔离用例间状态。"""
    _memory.clear()


__all__ = ["SAMPLE_TTL_SECONDS", "last_sample_payload", "remember_sample", "reset_memory"]
