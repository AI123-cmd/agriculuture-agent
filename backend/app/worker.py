"""ARQ worker 入口：定时投递发件箱 + 六个事件的任务注册。

与 api 进程分开部署（技术方案 15.1 的 `worker` 容器），启动方式：
`arq app.worker.WorkerSettings`。

用 ARQ 而非 Celery 是技术方案 4.5 的明确判断：应用全程 async，Celery 的同步
worker 需要额外的桥接层，ARQ 本身就是 asyncio 原生。

**通道 + 逐步落地的消费者**：六个任务名始终注册（事件照常入队、不丢不静默），未实现的
仍指向占位。T3-09 落地了第一个真实消费者 `prescription_created`（登记回访任务）和第一个
业务 cron `scan_due_followups`（每日到期扫描）；其余任务在各自阶段替换
`_placeholder_functions` 的对应项即可，投递路径与注册表无需改动。

各 cron 的职责不同：`dispatch_outbox` 每 5 秒投一轮发件箱（延迟敏感的通路）；
`scan_due_followups` 每天跑一次、横跨全部农户挑出到期回访并投 `followup.due`（回访按天计，
秒级扫描没有意义，反而放大日切边界上的重复投递）；`deliver_due_reach` 是 T3-12 的触达扫描，
**按农户时区在最佳窗口内加密、窗口外低频**（T3-13），逐条尝试推送到期提示条目并落触达记录。

`followup_due` 是 T3-12 落地的第二个真实消费者：`followup.due` 事件到达时立刻尝试一次主动
推送，不必等下一次触达扫描。它与 T3-08 的快照消费者共用同一注册项（事件到任务的映射是
1:1），本函数只落实"尝试推送"这一半，不影响首页提示。

`reach_failed` 是 T3-14 落地的第三个真实消费者：确认触达失败已落库并回执。失败信息在
**生产侧**就写进了 `reach_record`（`fail_reason` 四类），T6-09 起失败构成由 `MetricsRepository`
直接对 `reach_record` 聚合，故本消费者不再做进程内计数（那套计数会随进程重启丢失、无法跨进程
汇总，与"跨天对比"冲突）。

`collect_daily_metrics` 是 T6-09 落地的指标采集 cron：每自然日跑一次，把进程内的节点耗时/检查点
耗时直方图导出到 `eval_run`、构建当日抽检批次、并按需生成提示词回退工单（见
`app/observability/sampling.py`）。直方图在导出后即清空，故进程重启只丢未导出的样本、不会重复计数。

**时区**：`WorkerSettings.timezone` 设为农户时区（`Asia/Shanghai`），使全部 `hour`/`minute`
按北京时间而非服务器时区解释——否则容器默认 UTC，早晨窗口会被整体推后 8 小时。

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

from app.broadcast.notifier import deliver_due_items, notify_item
from app.broadcast.window import FARMER_TZ, is_sendable
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.events.handlers import UNHANDLED_TASK, registered_task_names
from app.events.outbox import OutboxDispatcher
from app.observability import configure_logging
from app.observability.sampling import collect_daily_metrics
from app.repositories import consult_repo, snapshot_repo
from app.services.followup_service import dispatch_due, register_from_prescription

logger = logging.getLogger(__name__)

# 投递 cron 的秒位。ARQ 的 cron 最小粒度是秒，用显式秒集合表达"每 5 秒一次"；
# 不用 `second={0, 5, ...}` 以外的写法是因为 ARQ 不支持 `*/5` 这类步进表达式。
_DISPATCH_SECONDS = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

# 每日到期扫描的时刻（UTC 小时）。取早上 7 点：落在产品说明书的最佳触达窗口
# （`BEST_WINDOWS` 的 6:30~7:30）之内，农户起床后能看到当天的回访提醒。
_DUE_SCAN_HOUR = 7

# 触达扫描的密集节奏：每 5 分钟一次，只在最佳窗口所在的小时触发。
#
# 为什么用"整点小时集合 + 任务内窗口判断"而不是把 cron 精确卡在 6:30~7:30 / 18:00~19:00：
# ARQ 的 cron 用"小时集合 + 分钟集合"两个正交维度表达，无法表达"6 点且分钟 >= 30"这类
# 跨维度约束；把窗口判断放进任务内（`in_best_window`）既精确又只需一条 cron。窗口外
# 的触发（如 6:00~6:25）由任务内的窗口判断直接返回，不产生任何记录。
_DELIVER_HOURS = {6, 7, 18}
_DELIVER_MINUTES = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

# 已落地真实实现的消费者任务名。从占位列表里剔除它们，避免同一任务名被注册两次
# （ARQ 后注册的会覆盖先注册的，顺序一旦变动就静默换实现）。
_IMPLEMENTED_TASKS: frozenset[str] = frozenset({"prescription_created", "followup_due", "reach_failed"})

# 每日指标采集的时刻（UTC 小时）。取凌晨 3 点：当天 0 点后就已"跨日"，此刻跑无需等待，
# 又能避开 7 点的到期扫描与 6/7/18 点的触达扫描，让三套 cron 在时间上互不重叠。
_METRICS_COLLECT_HOUR = 3


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


async def deliver_due_reach(ctx: dict[str, Any]) -> dict[str, int]:
    """cron 任务：在最佳触达窗口内扫描到期提示条目并逐条尝试推送（T3-12 / T3-13）。

    cron 按"窗口所在小时 + 每 5 分钟"触发（见 `_DELIVER_HOURS` / `_DELIVER_MINUTES`），但 ARQ
    的 cron 用"小时集合 + 分钟集合"两个正交维度表达，无法卡出"6 点且分钟 >= 30"这类跨维度
    约束，故把精确的窗口判定放进本函数：落在窗口外（如 6:00~6:25、7:30~7:59）直接返回，不触碰
    任何条目。

    为什么窗口外不跑扫描：`deliver_due_items` 的时段门槛会给每条候选写一条 `quiet_hours` 记录
    （可重试、不终结），若窗口外的每次触发都跑一遍，"因时段被延迟"的记录会成倍膨胀、把 T3-14 的
    送达率口径拉花。窗口外直接返回使 `quiet_hours` 只在"确实到期且被时段拦住"时产生，指标才有
    意义（T3-13）。

    自建会话但不显式 commit：`deliver_due_items` 的每条路径都自行 `commit`（见 `notifier.py`），
    本函数只负责开一个会话与日志。
    """
    now = datetime.now(FARMER_TZ)
    if not is_sendable(now):
        return {"delivered": 0, "skipped": 0, "failed": 0}
    async with SessionLocal() as db:
        stats = await deliver_due_items(db)
    if any(stats.values()):
        logger.info("触达扫描结果：%s", stats)
    return stats


async def followup_due(ctx: dict[str, Any], payload: dict | None = None, **kwargs: Any) -> int:
    """`followup.due` 的消费者（T3-12 落地的一半）：回查已落库条目并立即尝试推送一次。

    `followup.due` 有两个消费者（技术方案 6.6）：T3-08 的快照服务把它变成首页提示条目，本函数是
    T3-12 的触达服务那一半——事件到达时立刻尝试一次主动推送，不必等下一次触达扫描。两者互不阻塞
    （T3-09）：本函数不生成快照，也不修改任何提示条目状态（兜底五）。

    载荷只有 `task_id`（见 `followup_service._due_payload`），故推送对象按 `task_id` 回查已落库
    的 `prompt_item`（`rule_id` 后缀 `:{task_id}`）。查不到（快照尚未生成）即无操作——推送内容只能
    来自已落库条目（11.8），此刻没有条目就不推，交给触达扫描兜底。

    时段、额度、扣减与失败归类都在 `notify_item` 内（复用与扫描相同的一遍门槛），本函数不重复
    判断；单条推送的失败由 `notify_item` 落 `reach_record`，本函数再兜一层以防会话级异常。

    会话在此自建：ARQ 任务不在请求上下文里，事务边界由本函数负责；单条失败时回滚，避免污染
    后续条目的会话状态。
    """
    payload = payload or {}
    task_id = payload.get("task_id")
    if not task_id:
        logger.warning("followup.due 载荷缺少 task_id：%s", payload)
        return 0

    pushed = 0
    async with SessionLocal() as db:
        items = await snapshot_repo.list_items_by_followup_task(db, task_id)
        for item in items:
            try:
                await notify_item(db, item.id)
            except Exception as exc:  # noqa: BLE001  单条失败不中断整批，也不上抛（兜底五）
                logger.warning("followup.due 立即推送异常 item=%s：%s", item.id, exc)
                await db.rollback()
                continue
            pushed += 1
    if pushed:
        logger.info("followup.due 立即推送 %s 条（task=%s）", pushed, task_id)
    return pushed


async def reach_failed(ctx: dict[str, Any], payload: dict | None = None, **kwargs: Any) -> int:
    """`reach.failed` 的消费者（T3-14 / T6-09）：确认失败已落库，返回处理条数。

    `reach.failed` 的消费者是"指标服务"（技术方案 6.6）：失败原因的分类已由 `notifier` 在
    生产侧写进 `reach_record.fail_reason`，T6-09 起该表的失败构成由
    `MetricsRepository.subscribe_once_delivery_rate` 直接聚合，故本函数**不再写任何指标**——
    进程内计数器会随重启丢失、也无法跨 worker 汇总，与"跨天对比"的口径冲突。

    保留本函数是因为事件已投出就必须有消费者：删掉它会让 `reach.failed` 落到占位实现并印出
    "尚未实现"的日志，掩盖"这个事件其实已有归宿（`reach_record`）"这一事实。

    `quiet_hours` 不是失败（可重试的延迟），生产侧也**不投** `reach.failed`（见 T3-12 第三节），
    故这里只见到 `no_grant` / `quota_exhausted` / `api_error` 三类真实失败；载荷缺失
    `fail_reason` 时按 `unknown` 记日志，不静默丢弃。本消费者不接触数据库、不写任何表。
    """
    payload = payload or {}
    reason = payload.get("fail_reason") or "unknown"
    logger.info("触达失败已落库 reason=%s item=%s", reason, payload.get("prompt_item_id"))
    return 1


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


async def _on_startup(ctx: dict[str, Any]) -> None:
    """worker 启动钩子：装配日志链（T6-08）。

    api 进程在 lifespan 里配置日志，worker 是独立进程、不走 FastAPI 的 lifespan，故需自理。
    不配置的话，worker 日志既不是结构化 JSON，也不经脱敏——而 ARQ 任务的日志同样可能带
    设备 SN 与回访内容。
    """
    configure_logging(get_settings())


class WorkerSettings:
    """ARQ 配置。字段名由 ARQ 的 `create_worker` 按签名挑选，勿改名。"""

    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL or "redis://localhost:6379/0")
    # 显式标注：占位函数与具体任务函数的推断类型不同，不标注会被合并成
    # `list[object]`，下游取 `__qualname__` 就拿不到。
    functions: list[WorkerCoroutine] = [
        *_placeholder_functions(),
        prescription_created,
        followup_due,
        reach_failed,
        dispatch_outbox,
        scan_due_followups,
        deliver_due_reach,
        collect_daily_metrics,
    ]
    # 四套节奏：每 5 秒投一轮发件箱（`run_at_startup=True` 让 worker 起来先投一轮，
    # 不必干等到下一个整 5 秒边界）；每天 7 点扫描一次到期回访；触达扫描在最佳窗口所在
    # 的小时内每 5 分钟一次（窗口外由 `deliver_due_reach` 内的 `is_sendable` 直接返回）；
    # 每天 3 点采集一次线上指标并做当日抽检。
    cron_jobs = [
        cron(dispatch_outbox, second=_DISPATCH_SECONDS, run_at_startup=True),
        cron(scan_due_followups, hour=_DUE_SCAN_HOUR, minute=0),
        cron(deliver_due_reach, hour=_DELIVER_HOURS, minute=_DELIVER_MINUTES),
        cron(collect_daily_metrics, hour=_METRICS_COLLECT_HOUR, minute=0),
    ]
    # 单条任务的执行超时。投递本身只是入队，占位消费者也只写日志；回访登记与扫描
    # 涉及少量数据库写，给 60 秒远超需要，留足余量以容忍 Redis 抖动时的重连。
    job_timeout = 60
    # 保留任务结果 1 小时，便于排查"某条事件投出去之后发生了什么"。
    keep_result = 3600
    # 全部 `hour` / `minute` 按农户时区解释（`Asia/Shanghai`），否则容器默认 UTC，
    # 早晨窗口会被整体推后 8 小时（见模块注释"时区"）。
    timezone = FARMER_TZ
    # 启动钩子：装配日志链（T6-08）。worker 独立于 api 进程，需自行配置。
    on_startup = _on_startup


# 兜底任务名由注册表派生，此处仅作存在性断言：改注册表时若忘了同步，
# 这个断言会在导入期就把问题暴露出来（而不是等事件进来才 500）。
assert UNHANDLED_TASK in {fn.__qualname__ for fn in WorkerSettings.functions}


__all__ = [
    "WorkerSettings",
    "_on_startup",
    "collect_daily_metrics",
    "deliver_due_reach",
    "dispatch_outbox",
    "followup_due",
    "prescription_created",
    "reach_failed",
    "scan_due_followups",
]
