"""ARQ worker 入口：定时投递发件箱 + 六个事件的任务注册。

与 api 进程分开部署（技术方案 15.1 的 `worker` 容器），启动方式：
`arq app.worker.WorkerSettings`。

用 ARQ 而非 Celery 是技术方案 4.5 的明确判断：应用全程 async，Celery 的同步
worker 需要额外的桥接层，ARQ 本身就是 asyncio 原生。

**通道 + 逐步落地的消费者**：六个任务名始终注册（事件照常入队、不丢不静默），未实现的
仍指向占位。T3-09 落地了第一个真实消费者 `prescription_created`（登记回访任务）和第一个
业务 cron `scan_due_followups`（每日到期扫描）；其余任务在各自阶段替换
`_placeholder_functions` 的对应项即可，投递路径与注册表无需改动。

两个 cron 的职责不同：`dispatch_outbox` 每 5 秒投一轮发件箱（延迟敏感的通路），
`scan_due_followups` 每天跑一次、横跨全部农户挑出到期回访并投 `followup.due`（回访按天计，
秒级扫描没有意义，反而放大日切边界上的重复投递）。

**Windows 上的事件循环**：psycopg 的异步连接池不支持 ProactorEventLoop，
`app/main.py` 已在导入期切到 SelectorEventLoop；ARQ 的 CLI 会走 `asyncio.run`，
本模块在导入时做同样的切换，否则 `python -m arq` 在 Windows 上会直接报
"Psycopg cannot use the 'ProactorEventLoop'"。
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any, cast

# 必须在 ARQ 建事件循环之前设置：CLI 的 `asyncio.run` 会在导入 WorkerSettings
# 之后才跑起来，故这里设置仍然生效。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from arq import cron
from arq.connections import ArqRedis, RedisSettings
from arq.typing import WorkerCoroutine

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.events.handlers import UNHANDLED_TASK, registered_task_names
from app.events.outbox import OutboxDispatcher
from app.repositories import consult_repo
from app.services.followup_service import dispatch_due, register_from_prescription

logger = logging.getLogger(__name__)

# 投递 cron 的秒位。ARQ 的 cron 最小粒度是秒，用显式秒集合表达"每 5 秒一次"；
# 不用 `second={0, 5, ...}` 以外的写法是因为 ARQ 不支持 `*/5` 这类步进表达式。
_DISPATCH_SECONDS = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

# 每日到期扫描的时刻（UTC 小时）。取早上 7 点：落在产品说明书的最佳触达窗口
# （`BEST_WINDOWS` 的 6:30~7:30）之内，农户起床后能看到当天的回访提醒。
_DUE_SCAN_HOUR = 7

# 已落地真实实现的消费者任务名。从占位列表里剔除它们，避免同一任务名被注册两次
# （ARQ 后注册的会覆盖先注册的，顺序一旦变动就静默换实现）。
_IMPLEMENTED_TASKS: frozenset[str] = frozenset({"prescription_created"})


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


async def prescription_created(ctx: dict[str, Any], payload: dict | None = None, **kwargs: Any) -> int:
    """`prescription.created` 的消费者：登记该处方卡的全部回访任务，返回登记条数。

    载荷只带 `session_id`（即 `consultation.id`）与 `user_id`，回访登记需要的是处方卡
    （`followup_plan` / `consultation_id`），故按 `consultation_id` 反查。反查与登记都强制
    归属，因此 `payload["user_id"]` 与卡片归属必须一致，否则查不到卡、记一条告警后返回 0。

    会话在此自建并显式提交：ARQ 任务不在请求上下文里，没有 `get_db` 的兜底，事务边界由
    本函数负责。`register_from_prescription` 内部还会对已到期任务立即投递 `followup.due`，
    与登记在同一事务内，一起提交或一起回滚。
    """
    payload = payload or {}
    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    if not user_id or not session_id:
        logger.warning("prescription.created 载荷缺少 user_id/session_id：%s", payload)
        return 0

    async with SessionLocal() as db:
        card = await consult_repo.get_card_by_consultation(db, user_id, session_id)
        if card is None:
            logger.warning("处方卡未找到（session=%s user=%s），跳过回访登记", session_id, user_id)
            return 0
        tasks = await register_from_prescription(db, user_id, card)
        await db.commit()
        if tasks:
            logger.info("处方 %s 登记回访任务 %s 条", card.id, len(tasks))
        return len(tasks)


async def scan_due_followups(ctx: dict[str, Any]) -> int:
    """cron 任务：每日扫描全库到期回访并投递 `followup.due`，返回处理条数。

    与 `dispatch_outbox` 一样自建会话并提交：扫描是"读到期任务 + 写事件"的一组写入，
    必须在同一事务里完成，才能保证"事件与它引用的任务同时可见"。
    """
    async with SessionLocal() as db:
        count = await dispatch_due(db, datetime.now(UTC).date())
        await db.commit()
    if count:
        logger.info("每日到期扫描投递 %s 条回访提醒", count)
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
    """尚未实现的任务名的占位实现（含未注册事件的兜底任务，不含已落地的消费者）。"""
    return [_build_placeholder(name) for name in registered_task_names() if name not in _IMPLEMENTED_TASKS]


class WorkerSettings:
    """ARQ 配置。字段名由 ARQ 的 `create_worker` 按签名挑选，勿改名。"""

    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL or "redis://localhost:6379/0")
    # 显式标注：占位函数与具体任务函数的推断类型不同，不标注会被合并成
    # `list[object]`，下游取 `__qualname__` 就拿不到。
    functions: list[WorkerCoroutine] = [
        *_placeholder_functions(),
        prescription_created,
        dispatch_outbox,
        scan_due_followups,
    ]
    # 两套节奏：每 5 秒投一轮发件箱（`run_at_startup=True` 让 worker 起来先投一轮，
    # 不必干等到下一个整 5 秒边界）；每天 7 点扫描一次到期回访。
    cron_jobs = [
        cron(dispatch_outbox, second=_DISPATCH_SECONDS, run_at_startup=True),
        cron(scan_due_followups, hour=_DUE_SCAN_HOUR, minute=0),
    ]
    # 单条任务的执行超时。投递本身只是入队，占位消费者也只写日志；回访登记与扫描
    # 涉及少量数据库写，给 60 秒远超需要，留足余量以容忍 Redis 抖动时的重连。
    job_timeout = 60
    # 保留任务结果 1 小时，便于排查"某条事件投出去之后发生了什么"。
    keep_result = 3600


# 兜底任务名由注册表派生，此处仅作存在性断言：改注册表时若忘了同步，
# 这个断言会在导入期就把问题暴露出来（而不是等事件进来才 500）。
assert UNHANDLED_TASK in {fn.__qualname__ for fn in WorkerSettings.functions}


__all__ = [
    "WorkerSettings",
    "dispatch_outbox",
    "prescription_created",
    "scan_due_followups",
]
