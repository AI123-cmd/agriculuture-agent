"""快照生成主链路（T3-08，技术方案 11.1 / 11.4）。

本模块把"农户点一次「更新提示」"串成一条完整链路：

    Redis 幂等锁 → 幂等命中检查 → 三类输入采集 → 规则匹配 → 合并排序截断
    → 文案生成 → 文案安全校验 → 落库

四个设计要点：

1. **幂等保障分两层，锁只是「减少无效工作」**。唯一的强一致保障来自 T3-01 的唯一约束
   `(user_id, farm_entry_id, snapshot_date, revision)`；Redis 锁（`app/broadcast/lock.py`）
   只是把同一农户同地块的并发请求串起来，避免两个请求同时跑完整条生成链路。因此 `save`
   + `commit` 撞唯一约束时不是失败，而是回滚后把已存在的那份快照取出来返回——并发只落
   一行、所有调用返回同一 `snapshot_id`。
2. **同日重复点击做「差集合并」而非「整份重生成」**。重跑输入采集与规则，取命中集合与
   快照已有条目的 `rule_id` 差集：空则原样返回（"今日提示已是最新"），非空则只把新条目
   渲染、过闸门后挂到原快照上并自增 `revision`。**绝不整份替换**——那会让农户上午已勾选的
   `executed` 勾选状态丢失，农户会怀疑自己的操作没生效（产品说明书 4.3.3 功能点 1）。
3. **`PromptItem` 没有 `text` 列，渲染文案只在内存中作为 `RenderedItem` 传递**。文案由
   `writer.build_text` 生成，供闸门按"最终文案里的数字"做安全校验；落库的是五段式四段
   原文，不做文案回写（T3-01 的列定义即如此）。
4. **被压制的条目也要落库**。`rank_and_limit` 截断出的 `dropped` 条目以 `suppressed=True`
   落库，供产品复盘"哪些内容长期被压制"（T3-06）；它们无需渲染、不过闸门。

数据访问一律走仓储（守全局约定 L4，禁止 `db.query`）：快照走 `snapshot_repo`，档案走
`profile_repo`，三路输入各自的采集函数内部也走仓储。
"""

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import ProcessId
from app.broadcast.gate_check import RenderedItem, gate_check
from app.broadcast.inputs.calendar import collect_calendar_tasks, load_calendar_entries
from app.broadcast.inputs.consult import collect_consult_signals
from app.broadcast.inputs.env_facts import EnvFacts, collect_env_facts
from app.broadcast.limits import (
    MAX_ACTION_CHARS,
    MAX_CALLOUT_CHARS,
    MAX_FACT_CHARS,
    MAX_IMPACT_CHARS,
    MAX_ITEM_CHARS,
    SENTENCE_END,
    fit_item_text,
)
from app.broadcast.lock import redis_lock, snapshot_key
from app.broadcast.priority import merge_and_rank, priority_score, rank_and_limit
from app.broadcast.rules.base import RuleResult
from app.broadcast.rules.engine import RuleContext, RuleEngine
from app.broadcast.writer import build_text
from app.db.base import new_id
from app.db.redis_client import get_redis
from app.models import FarmerProfile, PromptItem, PromptSnapshot
from app.process_config.errors import ConfigLoadError
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig
from app.repositories import profile_repo, snapshot_repo

logger = logging.getLogger(__name__)

# 幂等锁的自动过期秒数，与快照生成预期耗时匹配（技术方案 11.4）。
LOCK_TIMEOUT_SECONDS = 30

# 频次提示的起始点击次数：同日第三次点击起才提示"明天会有新内容"。
NOTICE_CLICK_THRESHOLD = 3

# 点击计数键前缀。计数只服务频次文案，不落库（Redis 丢失的最坏后果只是文案不显示）。
CLICK_KEY_PREFIX = "snapshot_clicks"


def build_update_notice(click_count: int, merged_new: bool) -> str | None:
    """频次提示文案：同日第三次点击起提示今日已更新次数（产品说明书 4.3.3 功能点 1）。

    `merged_new` 表示本次是否真的并入了新信号；即便并入了新内容，第三次起仍提示"明天会有
    新内容"——产品意图是抑制反复刷新，与本次是否更新无关，故该参数不改变文案分支，仅保留
    在签名里供调用方表达语义。
    """
    if click_count < NOTICE_CLICK_THRESHOLD:
        return None
    return f"今日已更新 {click_count} 次，明天会有新内容"


def _redis_or_none() -> aioredis.Redis | None:
    """取 Redis 客户端，任何异常都折算为 `None`（等价于"不可用"，不做失败升级）。"""
    try:
        return get_redis()
    except Exception as exc:  # noqa: BLE001  不可用原因不决定分支，一律视为不可用
        logger.warning("获取 Redis 客户端失败，点击计数降级为不计数：%s", exc)
        return None


def _seconds_until_tomorrow(today: date) -> int:
    """距次日零点的秒数，作为计数键的 TTL（至少 1 秒，避免立即过期）。"""
    tomorrow = datetime.combine(today + timedelta(days=1), time.min)
    return max(int((tomorrow - datetime.now()).total_seconds()), 1)


async def register_click(user_id: str, today: date) -> int:
    """记一次点击并返回当日累计次数；Redis 不可用或异常时返回 0。

    键为 `snapshot_clicks:{user_id}:{date}`，过期设在次日零点。计数不落库的理由见模块注释：
    它只服务提示文案，不影响任何业务判定；Redis 计数丢失的最坏后果只是提示文案不显示，
    幂等正确性由数据库唯一约束保证。
    """
    client = _redis_or_none()
    if client is None:
        return 0
    key = f"{CLICK_KEY_PREFIX}:{user_id}:{today.isoformat()}"
    try:
        count = await client.incr(key)
        await client.expire(key, _seconds_until_tomorrow(today))
    except Exception as exc:  # noqa: BLE001  计数失败不影响主链路，按 0 处理
        logger.warning("点击计数失败（key=%s），本次不计数：%s", key, exc)
        return 0
    return int(count)


def _load_config(process_id: ProcessId) -> ProcessConfig | None:
    """取流程配置；配置不可用时返回 `None`（由调用方决定降级口径）。"""
    try:
        return get_process_config(process_id)
    except ConfigLoadError:
        return None


async def _collect_hits(
    db: AsyncSession, user_id: str, farm_entry_id: str | None, today: date
) -> tuple[EnvFacts, ProcessId, ProcessConfig | None, list[RuleResult]]:
    """三类输入采集 + 规则匹配的组合步骤，供首建与差集合并复用。

    返回 `(环境事实, 流程标识, 流程配置, 命中条目)`。`process_id` 由经营条目取得：匹配
    `farm_entry_id` 的那一条条目的流程即本次快照的流程，取不到（杂草等不绑定地块的流程）
    回落到通用农业。档案缺失时用未入库的占位档案，使农事历采集退化为"无地区信息"而不是崩。
    """
    profile = await profile_repo.get_for_user(db, user_id)
    if profile is None:
        profile = FarmerProfile(id=new_id("fp"))

    entries = await load_calendar_entries(db, user_id)
    process_id = next(
        (entry.process_id for entry in entries if entry.farm_entry_id == farm_entry_id),
        ProcessId.GENERAL,
    )

    facts = await collect_env_facts(user_id, farm_entry_id)
    calendar_tasks = await collect_calendar_tasks(profile, today, entries=entries, env=facts)
    consult_signals = await collect_consult_signals(db, user_id, today)
    config = _load_config(process_id)

    hits = RuleEngine().match_all(
        RuleContext(
            env=facts,
            process_id=process_id,
            process_config=config,
            calendar=calendar_tasks,
            signals=consult_signals,
        )
    )
    return facts, process_id, config, hits


def _make_item(user_id: str, result: RuleResult, *, suppressed: bool) -> PromptItem:
    """把一条规则命中结果转成待落库的 `PromptItem`。

    `snapshot_id` 不在此处填：首建路径由 `snapshot_repo.save` 统一补，合并路径由调用方
    挂到既有快照 id 上。四段与 `rule_id`、`facts_provenance` 直接取自 `RuleResult`（字段
    一一对应，落库不需要翻译）；`priority_score` 用 `str` 中转成 `Decimal`，避免浮点二进制
    误差落进 `Numeric(8, 3)`。
    """
    return PromptItem(
        id=new_id("item"),
        user_id=user_id,
        source_kind=result.source_kind,
        severity=result.severity,
        callout=result.callout,
        fact=result.fact,
        impact=result.impact,
        action=result.action,
        rule_id=result.rule_id,
        facts_provenance=dict(result.facts_provenance),
        priority_score=Decimal(str(priority_score(result))),
        suppressed=suppressed,
    )


def _effective_item_limit(config: ProcessConfig | None) -> int:
    """单条提示的有效字数上限（T4-05 四级优先级）。

    流程配置 `output_template.max_item_chars`（可覆盖，缺省即 `MAX_ITEM_CHARS`）优先，再与
    整条上限 `max_chars` **取较小值**——这里显式写出取小，不默认执行者会自行取小。
    """
    if config is None:
        return MAX_ITEM_CHARS
    template = config.output_template
    return min(template.max_item_chars, template.max_chars)


def _fit_segment(text: str, limit: int) -> tuple[str, bool]:
    """收敛单个字段；返回 `(收敛后文本, 是否发生有损硬切)`。

    "有损"专指整段无句末标点、只能硬切到上限的情形。此时句义被截断（农户会误以为内容
    没加载完），据 T3-01 对 `suppressed` 的定义如实标记，供复盘"哪些内容长期被压制"。
    退到句末标点的收敛是无损的（句子完整、只是更短），不标记。
    """
    stripped = text.strip()
    fitted = fit_item_text(text, limit)
    lossy = len(stripped) > limit and not (fitted and fitted[-1] in SENTENCE_END)
    return fitted, lossy


def _fit_result(result: RuleResult) -> tuple[RuleResult, bool]:
    """落库前逐段收敛，返回 `(收敛后的结果, 是否有段发生有损硬切)`。

    逐段上限固定为 `limits` 的四个常量，其和为 `MAX_ITEM_CHARS`，保证"逐段不超即总长不超"。
    收敛后的字段不回写原 `RuleResult`（frozen），而以 `dataclasses.replace` 生成新对象。
    """
    callout, l1 = _fit_segment(result.callout, MAX_CALLOUT_CHARS)
    fact, l2 = _fit_segment(result.fact, MAX_FACT_CHARS)
    impact, l3 = _fit_segment(result.impact, MAX_IMPACT_CHARS)
    action, l4 = _fit_segment(result.action, MAX_ACTION_CHARS)
    fitted = replace(result, callout=callout, fact=fact, impact=impact, action=action)
    return fitted, (l1 or l2 or l3 or l4)


async def _render_kept(
    db: AsyncSession,
    user_id: str,
    results: list[RuleResult],
    *,
    process_id: ProcessId,
    config: ProcessConfig | None,
    today: date,
    snapshot_id: str | None,
) -> list[PromptItem]:
    """对展示位条目出文案、逐段收敛并过闸门，返回通过校验、已挂好归属的 `PromptItem`。

    `snapshot_id` 非空时（合并路径）直接挂到既有快照上；为空时（首建路径）留给
    `snapshot_repo.save` 统一补。文案只作为 `RenderedItem` 传递给闸门，不回写 ORM 对象
    （`PromptItem` 无 `text` 列）。

    T4-05 的落库前收敛在此发生：`fact / impact / action` 以及 `callout` 逐段按上限收敛
    （`limits.MAX_*_CHARS`），有损硬切的条目置 `suppressed=True` 但不丢弃；整条文案
    的有效上限由 `_effective_item_limit` 给出并传给 `build_text`。
    """
    limit = _effective_item_limit(config)
    rendered: list[RenderedItem] = []
    for result in results:
        fitted, lossy = _fit_result(result)
        item = _make_item(user_id, fitted, suppressed=lossy)
        # 文案由**收敛后**的四段生成：闸门的溯源校验是"文案里的数字必须能在条目四段中找到
        # 登记"（见 gate_check._registered_tokens），故校验对象与落库展示的四段必须同源。
        text = await build_text(fitted, max_item_chars=limit)
        rendered.append(RenderedItem(item=item, text=text))

    kept = await gate_check(
        db,
        user_id,
        rendered,
        process_id=process_id.value,
        config_version=config.version if config is not None else 0,
        today=today,
    )
    if snapshot_id is not None:
        for item in kept:
            item.snapshot_id = snapshot_id
    return kept


async def _build_snapshot(db: AsyncSession, user_id: str, farm_entry_id: str | None, today: date) -> PromptSnapshot:
    """首建路径：三类输入 → 规则匹配 → 合并截断 → 文案 → 安全校验 → 待落库快照对象。

    展示位条目（`kept`）出文案并过闸门；被截断条目（`dropped`）只构造 `suppressed=True`
    的对象直接落库，不渲染也不校验——它们不进展示位，渲染与校验都无对象。
    """
    facts, process_id, config, hits = await _collect_hits(db, user_id, farm_entry_id, today)
    kept, dropped = rank_and_limit(hits)

    kept_items = await _render_kept(
        db,
        user_id,
        kept,
        process_id=process_id,
        config=config,
        today=today,
        snapshot_id=None,
    )
    dropped_items = [_make_item(user_id, result, suppressed=True) for result, _ in dropped]

    snapshot = PromptSnapshot(
        id=new_id("snap"),
        user_id=user_id,
        farm_entry_id=farm_entry_id,
        snapshot_date=today,
        generated_at=datetime.now(UTC),
        env_source=facts.source,
        revision=1,
        # T3-15：执行率需按流程分别统计（产品说明书 6.3），故落库时记下本次生成所用的流程。
        process_id=process_id.value,
    )
    # `items` 不是 ORM 列，是生成链路挂在对象上的待落库条目列表（见 `_attach_items`）。
    _attach_items(snapshot, kept_items + dropped_items)
    return snapshot


async def _merge_new_items(
    db: AsyncSession,
    user_id: str,
    farm_entry_id: str | None,
    existing: PromptSnapshot,
    today: date,
) -> PromptSnapshot | None:
    """同日重复点击的差集合并：无新信号返回 `None`，有则挂到既有快照并返回刷新后的快照。

    判据是 `rule_id` 差集：重跑三类输入与规则后，取命中集合与快照已有条目的 `rule_id` 集合
    之差。原有已勾选条目的 `executed` 状态原样保留——只追加、不替换（模块注释第二条）。
    """
    _facts, process_id, config, hits = await _collect_hits(db, user_id, farm_entry_id, today)
    existing_items = await snapshot_repo.list_items(db, user_id, existing.id)
    seen = {item.rule_id for item in existing_items}

    new_hits = [result for result in merge_and_rank(hits) if result.rule_id not in seen]
    if not new_hits:
        return None

    new_items = await _render_kept(
        db,
        user_id,
        new_hits,
        process_id=process_id,
        config=config,
        today=today,
        snapshot_id=existing.id,
    )
    for item in new_items:
        await snapshot_repo.add_item(db, user_id, item)

    new_revision = await snapshot_repo.bump_revision(db, user_id, existing.id)
    await db.commit()
    existing.revision = new_revision or existing.revision
    # 刷新后的条目列表含原有条目与本次新增，供接口层组装展示列表（未经 ORM 关系加载）。
    _attach_items(existing, await snapshot_repo.list_items(db, user_id, existing.id))
    return existing


def _attach_items(snapshot: PromptSnapshot, items: Sequence[PromptItem]) -> None:
    """把待落库/待展示的条目列表挂到快照对象上。

    `items` 不是 `PromptSnapshot` 的 ORM 列，而是生成链路在内存中挂在对象上的列表：
    `snapshot_repo.save` 按它级联写入条目，接口层按它组装展示列表。既有的取用方
    （仓储与 `broadcast_service`）都用 `getattr(snapshot, "items", ...)` 读取，故这里用
    `cast` 绕过 ORM 类上不存在该属性的类型检查，而不是新增一个 ORM 关系。
    """
    cast(Any, snapshot).items = items


async def generate_snapshot(db: AsyncSession, user_id: str, farm_entry_id: str | None) -> PromptSnapshot:
    """生成提示快照：同日同农户同地块幂等，只生成一份有效快照（技术方案 11.4）。

    并发兜底在 `save` + `commit` 上：撞唯一约束时回滚并取回已存在的那一行返回，保证并发
    只落一行、所有调用返回同一 `snapshot_id`。Redis 锁不可用时临界区照常执行（见 `lock.py`
    的层次说明），正确性仍由数据库唯一约束保证。
    """
    today = date.today()
    key = snapshot_key(user_id, farm_entry_id, today)

    async with redis_lock(key, timeout=LOCK_TIMEOUT_SECONDS):
        existing = await snapshot_repo.get_today(db, user_id, farm_entry_id, today)
        if existing is not None and not existing.invalidated:
            # 同日重复点击：合并去重后返回增强版，无新信号则原样返回。
            merged = await _merge_new_items(db, user_id, farm_entry_id, existing, today)
            return merged if merged is not None else existing

        snapshot = await _build_snapshot(db, user_id, farm_entry_id, today)
        try:
            await snapshot_repo.save(db, user_id, snapshot)
            await db.commit()
        except IntegrityError:
            # 唯一约束兜底：并发下另一请求已抢先落库，回滚后返回已存在的那一份。
            await db.rollback()
            winner = await snapshot_repo.get_today(db, user_id, farm_entry_id, today)
            if winner is not None:
                return winner
            raise
        return snapshot


__all__ = ["build_update_notice", "generate_snapshot", "register_click"]
