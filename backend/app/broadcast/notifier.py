"""主动触达链路：一次性订阅消息的发送与送达记录（T3-12 + T3-14，技术方案 11.6 / 11.7 / 11.8）。

本模块把"扫描到期提示条目 → 检查时段与额度 → 扣减额度 → 调微信接口 → 记录结果"串成一条
链路，是 `reach_record` 的生产方。三条不变量：

1. **推送内容只能来自已落库的提示条目**（11.8）。`notify_item` 的签名刻意**没有文案参数**，
   内容一律经 `snapshot_repo.get_item(prompt_item_id)` 从库中读取；第二层由
   `reach_record.prompt_item_id` 外键在数据层保证。因此"农户收到 X、首页显示 Y"在结构上
   不可能发生。
2. **四道门槛的顺序**（11.6）：先判时段、再判额度、再扣额度、最后调接口。时段最便宜且最常见
   （一天有 8 小时静默）；额度决定是否触碰外部接口，拦在自己系统里成本最低；扣额度在发送前
   是因为"先占用、失败再退还"才能避免"发送成功却扣额度失败"造成的透支。
3. **失败不得静默丢弃**（11.7 第三条）：每条路径都以 `record_attempt` 收尾，包括成功；静默
   时段记 `quiet_hours`（可重试、不退还），接口失败记 `api_error`（退还）、农户拒收记
   `no_grant`（清零额度、需重新授权）。

**兜底五（推送失败不影响已有功能）** 的两处落地：`deliver_due_items` 的整个循环逐条 try，
单条失败不影响后续条目；`record_attempt` 的调用也包在外层 try 内，即便记录写入失败（连接抖动）
推送流程仍正常返回统计，最坏后果是这一条观测记录丢失。`deliver_due_items` 返回统计而不抛异常。
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast.reach_log import TZ, record_attempt
from app.broadcast.window import is_sendable
from app.core.config import get_settings
from app.core.exceptions import NotFound
from app.events.handlers import EVENT_REACH_FAILED, EVENT_SUBSCRIPTION_CONSUMED
from app.events.outbox import emit
from app.integrations.wechat import WechatApiError, WechatUserRefusedError, send_subscribe_message
from app.models import PromptItem, ReachRecord
from app.models.enums import Channel, FailReason, ReachStatus
from app.repositories import quota_repo, reach_repo, snapshot_repo, user_repo
from app.services.quota_service import refund, try_consume

logger = logging.getLogger(__name__)

# 推送尝试一律记 `subscribe_once`：本模块只做主动推送这一条通道。`in_app`
# （仅小程序内提示位展示的降级路径）由别的生产方记录，不在此处混用。
_CHANNEL = Channel.SUBSCRIBE_ONCE.value

# 事件聚合根类型（`emit` 的 `aggregate_type`）。
_REACH_AGGREGATE = "reach"


def _build_template_data(item: PromptItem) -> dict:
    """从已落库的提示条目构造订阅消息模板字段。

    取值全部来自 `item`（`callout` / `fact` / `action`），不接受任何外部文案——这是
    "推送内容与首页显示一致"在发送侧的实现。字段名（`thing1` 等）对应微信模板定义。
    """
    return {
        "thing1": {"value": item.callout},
        "thing2": {"value": item.fact},
        "thing3": {"value": item.action},
    }


async def _send_via_wechat(db: AsyncSession, item: PromptItem) -> None:
    """把一条提示条目经一次性订阅消息发给条目归属的农户。

    内容只取自 `item`（`_build_template_data`）；openid 由 `item.user_id` 反查得到。
    发送失败抛 `WechatApiError` / `WechatUserRefusedError`，由调用方决定退还还是清零额度。
    """
    user = await user_repo.get(db, item.user_id)
    if user is None or not user.openid:
        raise WechatApiError(f"农户无可用 openid，无法推送：user={item.user_id}")

    settings = get_settings()
    await send_subscribe_message(
        openid=user.openid,
        data=_build_template_data(item),
        page=settings.WECHAT_SUBSCRIBE_PAGE or None,
        template_id=settings.WECHAT_SUBSCRIBE_TEMPLATE_ID or None,
    )


async def _emit_reach_event(
    db: AsyncSession, item: PromptItem, record: ReachRecord, *, failed: bool, fail_reason: str | None
) -> None:
    """投递触达相关事件：成功投 `subscription.consumed`，失败投 `reach.failed`。

    幂等键用 `reach_record.id`（全局唯一），因此同一记录不会重复投递；事件与记录同一事务提交。
    """
    if failed:
        event_type = EVENT_REACH_FAILED
        payload = {"prompt_item_id": item.id, "reach_record_id": record.id, "fail_reason": fail_reason}
    else:
        event_type = EVENT_SUBSCRIPTION_CONSUMED
        payload = {"prompt_item_id": item.id, "reach_record_id": record.id, "channel": _CHANNEL}
    await emit(
        db,
        event_type=event_type,
        aggregate_type=_REACH_AGGREGATE,
        aggregate_id=record.id,
        user_id=item.user_id,
        payload=payload,
        idempotency_key=f"{event_type}:{record.id}",
    )


async def _deliver_one(db: AsyncSession, item: PromptItem, now: datetime) -> ReachRecord:
    """对单条提示条目走完四道门槛，返回最终落库的触达记录（并提交本次写入）。

    返回的记录既是结果也是判据：`status="delivered"` 为成功，`fail_reason="quiet_hours"`
    为可重试的延迟，其余为真实失败。调用方据 `fail_reason` 归入统计桶。

    每条路径都以 `record_attempt` 收尾（含成功），这是"失败不得静默丢弃"（11.7 第三条）的
    落地方式；`user_id` / `prompt_item_id` / `channel` 三字段由本函数统一补，`record_attempt`
    是唯一的落库出口（T3-14）。
    """
    # 门槛一：时段。静默/非窗口时段只延迟，不退还额度（尚未消费）、不投 reach.failed。
    if not is_sendable(now):
        record = await record_attempt(
            db,
            user_id=item.user_id,
            prompt_item_id=item.id,
            channel=_CHANNEL,
            fail_reason=FailReason.QUIET_HOURS.value,
        )
        await db.commit()
        return record

    # 门槛二：额度。`try_consume` 内部提交，故无额度时先补记再提交记录。
    if not await try_consume(db, item.user_id):
        # 再查一次以区分"从未授权"与"授权已用完"（两者对农户的界面说明不同）。
        quota = await quota_repo.get(db, item.user_id)
        granted_total = quota.granted_total if quota is not None else 0
        reason = FailReason.NO_GRANT.value if granted_total == 0 else FailReason.QUOTA_EXHAUSTED.value
        record = await record_attempt(
            db,
            user_id=item.user_id,
            prompt_item_id=item.id,
            channel=_CHANNEL,
            fail_reason=reason,
        )
        await _emit_reach_event(db, item, record, failed=True, fail_reason=reason)
        await db.commit()
        return record

    # 门槛三：额度已在 try_consume 内提交（"先占用"落库）。门槛四：调接口。
    try:
        await _send_via_wechat(db, item)
    except WechatUserRefusedError as exc:
        # 农户拒收：系统持有的授权已失效，清零剩余额度并记 no_grant，避免反复失败。
        await quota_repo.zero_out(db, item.user_id)
        record = await record_attempt(
            db,
            user_id=item.user_id,
            prompt_item_id=item.id,
            channel=_CHANNEL,
            fail_reason=FailReason.NO_GRANT.value,
            note=str(exc),
        )
        await _emit_reach_event(db, item, record, failed=True, fail_reason=FailReason.NO_GRANT.value)
        await db.commit()
        return record
    except Exception as exc:  # noqa: BLE001  含 WechatApiError 与任何非预期异常，一律记 api_error
        # 接口失败（含未预期异常）：退还占用，记 api_error，投 reach.failed，下次扫描重试。
        await refund(db, item.user_id)
        record = await record_attempt(
            db,
            user_id=item.user_id,
            prompt_item_id=item.id,
            channel=_CHANNEL,
            fail_reason=FailReason.API_ERROR.value,
            note=str(exc),
        )
        await _emit_reach_event(db, item, record, failed=True, fail_reason=FailReason.API_ERROR.value)
        await db.commit()
        return record

    record = await record_attempt(
        db,
        user_id=item.user_id,
        prompt_item_id=item.id,
        channel=_CHANNEL,
        fail_reason=None,
    )
    await _emit_reach_event(db, item, record, failed=False, fail_reason=None)
    await db.commit()
    return record


def _bucket(record: ReachRecord) -> str:
    """把一条触达记录归入统计桶：延迟计入 `skipped`，失败计入 `failed`，成功计入 `delivered`。"""
    if record.status == ReachStatus.DELIVERED.value:
        return "delivered"
    if record.fail_reason == FailReason.QUIET_HOURS.value:
        return "skipped"
    return "failed"


async def notify_item(db: AsyncSession, prompt_item_id: str, *, now: datetime | None = None) -> ReachRecord:
    """推送一条提示条目：内容只能来自 `prompt_item`，不接受直接传文案。

    签名刻意没有文案参数（11.8 顺序约束的第一层）。条目不存在时抛 `NotFound`。函数返回落库的
    触达记录；失败不抛异常（失败信息在记录的 `fail_reason` 里），唯一例外是条目不存在。
    """
    item = await snapshot_repo.get_item(db, prompt_item_id)
    if item is None:
        raise NotFound(f"提示条目不存在：{prompt_item_id}")
    return await _deliver_one(db, item, now or datetime.now(TZ))


async def deliver_due_items(db: AsyncSession, limit: int = 200) -> dict[str, int]:
    """扫描待推送条目并逐条尝试送达，返回各结果的计数（不抛异常）。

    静默时段只延迟（计入 `skipped`），接口失败与无额度计入 `failed`，成功计入 `delivered`。
    整个循环逐条 try：单条失败（含记录写入失败）不影响后续条目，函数自身返回统计而不冒泡，
    这是兜底五"推送失败不影响已有功能"的落地。
    """
    stats = {"delivered": 0, "skipped": 0, "failed": 0}
    now = datetime.now(TZ)
    candidates: Sequence[PromptItem] = await reach_repo.list_unpushed_due(db, now, limit)
    for item in candidates:
        try:
            record = await _deliver_one(db, item, now)
        except Exception as exc:  # noqa: BLE001  单条失败不中断整批，最坏是该条观测记录丢失
            logger.warning("触达记录写入或推送异常，跳过该条目 item=%s：%s", item.id, exc)
            await db.rollback()
            stats["failed"] += 1
            continue
        stats[_bucket(record)] += 1
    return stats


__all__ = ["deliver_due_items", "notify_item"]
