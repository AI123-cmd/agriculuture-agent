"""ARQ worker 入口：定时投递发件箱 + 六个事件的任务注册。

与 api 进程分开部署（技术方案 15.1 的 `worker` 容器），启动方式：
`arq app.worker.WorkerSettings`。

用 ARQ 而非 Celery 是技术方案 4.5 的明确判断：应用全程 async，Celery 的同步
worker 需要额外的桥接层，ARQ 本身就是 asyncio 原生。

**本项只建通道，消费者实现留给后续阶段**（T3-08 至 T6-09）。六个任务因此暂时
都指向占位实现：事件照常入队、照常记日志，`event_outbox.status` 因而仍然是
"事件已送达队列"的真实指标，不会因为消费者未实现而失真。真正的实现在各自任务
里替换 `_placeholders` 的对应项即可，投递路径与注册表无需改动。

**Windows 上的事件循环**：psycopg 的异步连接池不支持 ProactorEventLoop，
`app/main.py` 已在导入期切到 SelectorEventLoop；ARQ 的 CLI 会走 `asyncio.run`，
本模块在导入时做同样的切换，否则 `python -m arq` 在 Windows 上会直接报
"Psycopg cannot use the 'ProactorEventLoop'"。
"""

import asyncio
import logging
import sys
from typing import Any, cast

# 必须在 ARQ 建事件循环之前设置：CLI 的 `asyncio.run` 会在导入 WorkerSettings
# 之后才跑起来，故这里设置仍然生效。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from arq import cron
from arq.connections import ArqRedis, RedisSettings
from arq.typing import WorkerCoroutine

from app.core.config import get_settings
from app.events.handlers import UNHANDLED_TASK, registered_task_names
from app.events.outbox import OutboxDispatcher

logger = logging.getLogger(__name__)

# 投递 cron 的秒位。ARQ 的 cron 最小粒度是秒，用显式秒集合表达"每 5 秒一次"；
# 不用 `second={0, 5, ...}` 以外的写法是因为 ARQ 不支持 `*/5` 这类步进表达式。
_DISPATCH_SECONDS = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}


async def dispatch_outbox(ctx: dict[str, Any]) -> int:
    """cron 任务：跑一轮发件箱投递，返回本轮处理条数。

    复用 worker 自己的 Redis 连接池（`ctx["redis"]`），而不是每轮新建：新建池会
    在每次 cron 触发时多一次 TCP+认证握手，且连接无法复用。队列名也由池决定，
    与 `enqueue_job` 的目标队列天然一致，不会出现"投递到 A 队列、worker 听 B 队列"。
    """
    pool: ArqRedis | None = ctx.get("redis")
    count = await OutboxDispatcher().dispatch_once(pool=pool)
    if count:
        logger.info("发件箱本轮投递 %s 条", count)
    return count


async def _placeholder(ctx: dict[str, Any], payload: dict | None = None, **kwargs: Any) -> None:
    """未实现消费者的占位任务：只记日志。

    存在的意义是让"事件已到达队列"与"消费者已实现"两件事可以分开推进：没有它，
    六个事件要么投不出去、要么投出去抛 `KeyError` 被当成投递失败反复重试。
    """
    name = ctx.get("job_name") or "unknown"
    logger.info("事件消费者尚未实现，暂存不处理：%s payload=%s", name, payload)


def _build_placeholder(name: str) -> WorkerCoroutine:
    """为每个任务名生成具名占位函数。

    ARQ 用 `__qualname__` 作为任务名，匿名 lambda 会让六个任务全都叫
    `<lambda>`，注册表随即失效。这里显式设置 `__qualname__` 让名字稳定，
    也便于在日志里看出是哪条事件。
    """

    async def _task(ctx: dict[str, Any], payload: dict | None = None, **kwargs: Any) -> None:
        await _placeholder({**ctx, "job_name": name}, payload, **kwargs)

    _task.__qualname__ = name
    _task.__name__ = name
    return cast(WorkerCoroutine, _task)


def _placeholder_functions() -> list[WorkerCoroutine]:
    """全部任务名的占位实现（含未注册事件的兜底任务）。"""
    return [_build_placeholder(name) for name in registered_task_names()]


class WorkerSettings:
    """ARQ 配置。字段名由 ARQ 的 `create_worker` 按签名挑选，勿改名。"""

    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL or "redis://localhost:6379/0")
    # 显式标注：占位函数与 `dispatch_outbox` 的推断类型不同，不标注会被合并成
    # `list[object]`，下游取 `__qualname__` 就拿不到。
    functions: list[WorkerCoroutine] = [*_placeholder_functions(), dispatch_outbox]
    # 每 5 秒跑一轮投递。`run_at_startup=True` 让 worker 起来就先投一轮，
    # 不必干等到下一个整 5 秒边界。
    cron_jobs = [cron(dispatch_outbox, second=_DISPATCH_SECONDS, run_at_startup=True)]
    # 单条任务的执行超时。投递本身只是入队，占位消费者也只写日志，
    # 给 60 秒已远超需要，留足余量以容忍 Redis 抖动时的重连。
    job_timeout = 60
    # 保留任务结果 1 小时，便于排查"某条事件投出去之后发生了什么"。
    keep_result = 3600


# 兜底任务名由注册表派生，此处仅作存在性断言：改注册表时若忘了同步，
# 这个断言会在导入期就把问题暴露出来（而不是等事件进来才 500）。
assert UNHANDLED_TASK in {fn.__qualname__ for fn in WorkerSettings.functions}


__all__ = ["WorkerSettings", "dispatch_outbox"]
