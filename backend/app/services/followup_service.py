"""回访调度服务（T3-09，技术方案 11.10）。

两条链路：

1. **登记**：`prescription.created` 的消费者在处方落库后调用 `register_from_prescription`，
   按处方卡上的 `followup_plan` 为每一次回访登记一条 `followup_task`。任务里存的是**绝对
   时点的 `due_at`**，而处方契约给的是相对天数 `due_offset_days`——转换只在这里做一次，
   之后的到期扫描是纯时间范围查询，不需要再算基准日（`app/repositories/followup.py` 的
   模块注释）。
2. **到期**：`list_due` 供 T3-04 取某农户当日到期任务；`dispatch_due` 供每日 cron 横跨全部
   农户挑出到期任务并投递 `followup.due`；`complete` 回填关闭。

**天数不在代码里写死**（验收要求 `grep after_days` 无输出）：回访节奏来自各流程 YAML 的
`followup_policy.triggers`，经 T2-21 的 `build_followup_plan` 变成处方卡上的 `followup_plan`。
本服务只做"相对天数 → 绝对时点"的换算与茶流程/水稻窗口的两处特例。

两处特例：

- **茶**：时点取"安全间隔期结束"与"下一轮采摘计划"两者中较晚的一个。安全间隔期未过不能采，
  但下一轮采摘本就晚于间隔期结束时，回访应跟着采摘计划走——那时农户才有"要不要采"的实际
  决策（T3-09 第一节）。
- **水稻（`window_based=True`）**：时点不是固定天数，而是"下一次施药的窗口开始前"，由农事历
  给出下一节点日期、`due_at` 设为该节点前一天。农事历不可用时回落到配置的相对天数——这条
  回落让水稻在无农事历的地区仍能产出回访，而不是整条丢失。

药害分支（`branch == "herbicide_damage"`）的 `due_offset_days=0` 由 T2-21 在计划里给出，
本服务据此登记为当天到期并**立即投递一次 `followup.due`**：现场固定动作若延迟到次日扫描，
现场可能已被农户处理掉（拔除、翻耕），提示价值归零。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agents.followup_builder import build_followup_plan
from app.agent.contracts import FollowupPlan, FollowupTask as FollowupTaskContract
from app.agent.state import ProcessId
from app.db.base import new_id
from app.events.handlers import EVENT_FOLLOWUP_DUE
from app.events.outbox import emit
from app.models import FollowupTask, PrescriptionCard
from app.models.enums import FollowupStatus
from app.repositories import consult_repo, followup_repo

# `ask` 的兜底文案。契约允许 `ask` 为空，而 `followup_task.ask` 是 NOT NULL；给一个中性
# 的询问语比留空更符合"回访要有话说"。流程配置的 triggers 都带 `ask`，这条只在异常数据上生效。
_DEFAULT_ASK = "上次给的做法效果如何，情况有没有变化"


def _at(day: date) -> datetime:
    """把日期落成当日零点的 UTC 时间戳。

    `due_at` 列带时区，回访按"天"计，取当日零点即可；用 UTC 而非本地零点是为了与
    `datetime.now(UTC)` 的比较口径一致（`due_at <= now` 的立即触发判定）。
    """
    return datetime.combine(day, time.min, tzinfo=UTC)


def _load_plan(card: PrescriptionCard) -> FollowupPlan | None:
    """从处方卡的 JSONB `followup_plan` 还原契约对象；无计划返回 `None`。

    还原而非直接当字典用：`FollowupPlan` 的校验器保证任务按时间升序，后续登记不必再排序，
    且字段名写错会在还原时暴露（`extra="forbid"`）。
    """
    if not card.followup_plan:
        return None
    return FollowupPlan.model_validate(card.followup_plan)


def _has_tea_dates(card: PrescriptionCard) -> bool:
    """处方卡上是否有可用于茶流程回访的日期（安全间隔期或下一轮采摘计划）。"""
    return card.safety_interval_days is not None or card.next_pick_date is not None


def _resolve_due_at(
    task: FollowupTaskContract,
    *,
    process_id: str | None,
    card: PrescriptionCard,
    base: date,
    window_date: date | None,
) -> datetime:
    """把一条回访任务的相对时点换算成绝对 `due_at`。

    判定顺序即优先级：茶流程的"安全间隔期 + 采摘计划"最具体，先于通用的窗口回落；窗口型
    任务在有农事历节点时取"节点前一天"，否则回落到配置的相对天数；其余任务一律
    `base + due_offset_days`。
    """
    if process_id == ProcessId.TEA and _has_tea_dates(card):
        candidates: list[date] = []
        if card.safety_interval_days is not None:
            candidates.append(base + timedelta(days=card.safety_interval_days))
        if card.next_pick_date is not None:
            candidates.append(card.next_pick_date)
        return _at(max(candidates))
    if task.window_based and window_date is not None:
        return _at(window_date - timedelta(days=1))
    return _at(base + timedelta(days=task.due_offset_days))


async def _emit_immediate(db: AsyncSession, user_id: str, tasks: Sequence[FollowupTask]) -> None:
    """对已到期的任务立即投递 `followup.due`（不等次日扫描）。

    药害分支的 T+0 提醒靠这里落地。`emit` 只 `add` 不开新事务，因此立即事件与任务登记在同一
    事务里：任务回滚则事件一并消失，不会出现"有事件无任务"。
    """
    now = datetime.now(UTC)
    for task in tasks:
        if task.due_at > now:
            continue
        await emit(
            db,
            event_type=EVENT_FOLLOWUP_DUE,
            aggregate_type="followup",
            aggregate_id=task.id,
            user_id=user_id,
            payload={
                "task_id": task.id,
                "consultation_id": task.consultation_id,
                "kind": task.kind,
                "object_label": task.object_label,
            },
        )


async def register_from_prescription(
    db: AsyncSession,
    user_id: str,
    card: PrescriptionCard,
    *,
    today: date | None = None,
    window_date: date | None = None,
) -> list[FollowupTask]:
    """按处方卡登记回访任务；本函数是 `prescription.created` 的消费者核心（不提交）。

    `object_label` 与流程类型取自卡片所属问诊（`consultation`）：卡片自身不带这两项，而
    `object_label` 是 NOT NULL、茶流程的时点换算需要流程类型。两者都要带归属过滤，故走
    `consult_repo.get`。

    `today` / `window_date` 可注入：前者固定相对天数的基准日，后者给水稻窗口型任务的农事历
    节点日期（缺省即"农事历不可用"，回落配置天数）。两者都只为测试与将来的农事历接入留位。
    """
    plan = _load_plan(card)
    if plan is None or not plan.tasks:
        return []

    consult = await consult_repo.get(db, user_id, card.consultation_id)
    process_id = consult.process_id if consult is not None else None
    object_label = consult.object_label if consult is not None else ""
    # 基准日取 UTC 当天而非本地 `date.today()`：`due_at` 与立即触发判定都用 UTC 时点，
    # 若基准日按本地日期取，跨过 UTC 零点的那几小时会让 T+0 任务的 `due_at` 落到未来，
    # 立即提醒被推迟到次日扫描——正是本节要消除的延迟。
    base = today or datetime.now(UTC).date()

    tasks = [
        FollowupTask(
            id=new_id("fu"),
            user_id=card.user_id,
            consultation_id=card.consultation_id,
            prescription_card_id=card.id,
            kind=task.kind,
            object_label=object_label,
            ask=task.ask or _DEFAULT_ASK,
            content_rule=task.content_rule,
            due_at=_resolve_due_at(
                task, process_id=process_id, card=card, base=base, window_date=window_date
            ),
            status=FollowupStatus.PENDING.value,
        )
        for task in plan.tasks
    ]

    saved = await followup_repo.bulk_save(db, tasks)
    await _emit_immediate(db, user_id, saved)
    return list(saved)


async def list_due(db: AsyncSession, user_id: str, today: date) -> Sequence[FollowupTask]:
    """某农户当日到期未回访的任务（T3-04 首页提示的数据来源）。"""
    return await followup_repo.list_due(db, user_id, today)


async def dispatch_due(db: AsyncSession, today: date) -> int:
    """每日扫描：横跨全部农户挑出到期任务并投递 `followup.due`，返回条数。

    幂等键带上日期（`followup.due:{task_id}:{today}`）：同一条任务在**同日**只投一次，
    次日仍 pending 则再投一次（每日提醒是期望行为）。不带日期会让次日扫描撞唯一约束、
    整批事务失败。
    """
    tasks = await followup_repo.list_due_all(db, today)
    for task in tasks:
        await emit(
            db,
            event_type=EVENT_FOLLOWUP_DUE,
            aggregate_type="followup",
            aggregate_id=task.id,
            user_id=task.user_id,
            payload={
                "task_id": task.id,
                "consultation_id": task.consultation_id,
                "kind": task.kind,
                "object_label": task.object_label,
            },
            idempotency_key=f"{EVENT_FOLLOWUP_DUE}:{task.id}:{today.isoformat()}",
        )
    return len(tasks)


async def complete(
    db: AsyncSession, user_id: str, task_id: str, *, result: str, note: str | None = None
) -> int:
    """回填结果并关闭任务，返回受影响行数（0 表示不存在或不属于该农户）。"""
    return await followup_repo.mark_done(db, user_id, task_id, result=result, note=note)


__all__ = [
    "build_followup_plan",
    "complete",
    "dispatch_due",
    "list_due",
    "register_from_prescription",
]
