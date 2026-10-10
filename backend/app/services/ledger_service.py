"""农事台账与执行回执服务（T3-15，技术方案 13.1 与产品说明书 4.5.2 / 4.3.3）。

本模块把"农户自己的农事记录"沉淀下来，两条写入路径共用一个目标：处方卡是"AI 建议我做什么"，
执行勾选是"我实际做了什么"，两者合起来构成产品说明书 4.5.2 功能点 2 所说的"这一季我做过
什么"的完整视图。

三块职责：

1. `save_prescription_to_ledger`：把处方卡存入台账，幂等键 `(user_id, source_type, source_id)`。
   台账条目的字段从处方卡**复制而非引用**——知识条目会随版本迭代 `deprecated`，若只存
   `knowledge_id`，未来回看时会因条目废弃而显示不出当时的药剂名。台账的价值在于沉淀历史，
   必须自包含（`app/models/ledger.py` 的模块注释详述）。
2. `set_item_executed`：执行勾选回执，`executed` 是北极星指标"执行率"的唯一数据来源。
3. `execution_rate` / `ignored_streak`：北极星指标与"连续未勾选"信号的统计口径。

**归属校验一律走仓储**（守全局约定 L4，`db.query` 在服务层与接口层都不出现）：`user_id`
只从令牌取（路由层 `get_current_user`），不接受请求体传入。`mark_done` 时代那种"手工拼
`item_id` + `user_id` 两个条件"的写法被替换——一旦有人复制到新接口时漏掉 `user_id` 就越权了；
改为走 `OwnedRepository` 后，归属条件是基类强制的，漏写会直接抛 `MissingOwnerError`。

**为什么执行率必须按流程分别统计**：产品说明书 6.3 第三层明确"上述指标需按流程分别统计"，
混在一起会掩盖单条流程的退化。因此 `process_id` 不是可选的便利参数，而是统计维度——不传时
按流程分组返回字典，而不是返回一个总数。

**未勾选同样是信号**：产品说明书 4.3.3 功能点 4 指出"连续多条提示未被勾选，说明提示内容与
农户实际需求不匹配，需要复盘规则与文案"。`ignored_streak` 把这句话落成一个可观测的数值。
"""

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast.window import FARMER_TZ
from app.core.exceptions import InvalidStateError, NotFound
from app.db.base import new_id
from app.db.uow import unit_of_work
from app.models import LedgerEntry, PrescriptionCard, PromptItem
from app.models.enums import LedgerSourceType
from app.repositories import ledger_repo, prescription_repo, snapshot_repo

# 执行勾选时间戳一律按农户时区落库（与 `app/broadcast/reach_log.py` 的 `TZ` 同口径）：
# `executed_at` 要与展示/触达时段（按农户本地时区判定）直接对照，用 UTC 会偏移 8 小时，
# 使"这条为什么在半夜被勾选"无法解释。
TZ = FARMER_TZ


def _actions_of(card: PrescriptionCard) -> list[dict]:
    """处方卡的处置动作快照（自包含拷入台账，不引用 `knowledge_id`）。"""
    return list(card.actions or [])


def _pesticides_of(card: PrescriptionCard) -> list[dict]:
    """处方卡的药剂条目快照（含药剂名与剂量，台账展示药剂名依赖它）。"""
    return list(card.pesticide_items or [])


def _has_followup(card: PrescriptionCard) -> bool:
    """该处方是否登记了回访，供台账展示"待回访"状态。

    判据是 `followup_plan.tasks` 非空：无回访计划的处方（如转人工降级）不登记回访，
    台账条目标记为不需要回访。
    """
    plan = card.followup_plan or {}
    return bool(plan.get("tasks"))


async def save_prescription_to_ledger(db: AsyncSession, user_id: str, card_id: str) -> LedgerEntry:
    """处方卡存入农事台账：幂等，重复点击返回既有条目。

    幂等键是 `(user_id, source_type, source_id)`，`source_id` 对处方卡就是 `card.id`。农户会
    重复点击"存入台账"（尤其当他没看到明确反馈时），若每次都新增一条，台账里会出现同一份处方
    的多条重复记录。命中既有条目时直接返回，不重复入账、也不重复回写 `ledger_saved`。

    归属校验在仓储内：`prescription_repo.get` 读不到他人卡片返回 `None`（不区分"不存在"与
    "不属于你"），此处折算为 404。`ledger_saved` 回写后，前端据此把按钮改为"已存入台账"状态，
    减少重复点击。
    """
    async with unit_of_work(db, name="ledger.save_prescription"):
        card = await prescription_repo.get(db, user_id, card_id)
        if card is None:
            raise NotFound("处方卡不存在")

        existing = await ledger_repo.get_by_source(
            db, user_id, source_type=LedgerSourceType.PRESCRIPTION.value, source_id=card_id
        )
        if existing is not None:
            return existing  # 幂等：不重复入账

        entry = LedgerEntry(
            id=new_id("lg"),
            user_id=user_id,
            entry_date=card.created_at.date(),
            process_id=card.process_id,
            source_type=LedgerSourceType.PRESCRIPTION.value,
            source_id=card_id,
            title=card.diagnosis_summary,
            actions=_actions_of(card),
            pesticides=_pesticides_of(card),
            safety_interval_days=card.safety_interval_days,
            facts_provenance=dict(card.facts_provenance or {}),
            followup_needed=_has_followup(card),
        )
        await ledger_repo.save(db, entry)
        await prescription_repo.mark_ledger_saved(db, user_id, card_id)  # 回写 ledger_saved
        return entry


async def set_item_executed(db: AsyncSession, user_id: str, item_id: str, executed: bool) -> PromptItem:
    """勾选或取消勾选提示条目：`executed` 是北极星指标的数据来源。

    接口一次点击完成（产品说明书 4.3.3 功能点 4 要求"勾选动作必须极简（一次点击）"）。
    取消时清空 `executed_at` 而不是保留旧时间戳——若保留，按 `executed_at` 落入时段的统计会把
    一条已取消的记录算成"当时执行了"，导致指标虚高；清空后 `executed=False` 与
    `executed_at IS NULL` 始终一一对应，统计条件只需 `WHERE executed = true`。

    被抑制的条目拒绝勾选：`suppressed=True` 的条目从未展示给农户，农户不可能勾选它；允许则会
    出现"农户勾选了不存在的提示"这一无法解释的数据。前端正常拿不到其 id，但接口必须自行拦截，
    不能依赖前端。
    """
    async with unit_of_work(db, name="ledger.set_item_executed"):
        item = await snapshot_repo.get_item_for_user(db, user_id, item_id)
        if item is None:
            raise NotFound("提示条目不存在")
        if item.suppressed:
            raise InvalidStateError("被抑制的条目不接受执行勾选")
        item.executed = executed
        item.executed_at = datetime.now(TZ) if executed else None
        await snapshot_repo.save_item(db, item)
        return item


async def list_ledger(
    db: AsyncSession,
    user_id: str,
    *,
    process_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[LedgerEntry]:
    """取农户的台账条目，按 `entry_date` 倒序（无生育期流程的纯时间线亦然）。

    `process_id` 可选过滤。产品说明书 4.5.2 功能点 2 指出"杂草防治与通用农业流程无生育期节点，
    改为按防除/处置时间线呈现"——本方法对这类流程不做特殊分支：台账没有生育期节点列，所有流程
    在库里都是同一条"按农事日期排列"的时间轴，过滤与否只影响取哪几条。
    """
    return await ledger_repo.list_by_date_desc(
        db, user_id, process_id=process_id, limit=limit, offset=offset
    )


def _rate(executed: int, shown: int) -> float:
    return executed / shown if shown else 0.0


async def execution_rate(
    db: AsyncSession, since: date, process_id: str | None = None
) -> dict:
    """执行率：按流程分别统计，混在一起会掩盖单条流程的退化。

    `since` 起、只计未被抑制的展示条目（`suppressed=False`）：被抑制条目从未展示，计入分母会
    把执行率人为压低，它反映的"系统主动隐藏了多少"属于另一个指标（T3-06 的复盘用途）。

    传 `process_id` 返回 `{"shown", "executed", "rate"}`；不传则按流程分组返回 `{流程: 上述字典}`，
    并含 `unknown` 键收纳 `process_id` 为空的历史快照，使"取不到流程"的条目可见而非被静默丢弃。
    """
    if process_id is None:
        shown = await snapshot_repo.count_items_by_process(db, since, suppressed=False)
        executed = await snapshot_repo.count_items_by_process(db, since, suppressed=False, executed=True)
        return {
            pid: {"shown": count, "executed": executed.get(pid, 0), "rate": _rate(executed.get(pid, 0), count)}
            for pid, count in shown.items()
        }

    shown = await snapshot_repo.count_items(db, since, process_id, suppressed=False)
    executed = await snapshot_repo.count_items(db, since, process_id, suppressed=False, executed=True)
    return {"shown": shown, "executed": executed, "rate": _rate(executed, shown)}


async def ignored_streak(db: AsyncSession, user_id: str, *, limit: int = 50) -> int:
    """某农户最近连续未被勾选的展示条目数（T6-09 指标采集的触发条件）。

    从最近展示的条目向前数，遇到第一条已勾选即停。被抑制条目不计入（它们从未展示）。
    这个数是产品说明书 4.3.3 功能点 4"连续多条提示未被勾选"的观测口径，使那句话成为一个可
    触发的数值，而不是一句原则。
    """
    items = await snapshot_repo.list_display_items_desc(db, user_id, limit=limit)
    streak = 0
    for item in items:
        if item.executed:
            break
        streak += 1
    return streak


__all__ = [
    "TZ",
    "execution_rate",
    "ignored_streak",
    "list_ledger",
    "save_prescription_to_ledger",
    "set_item_executed",
]
