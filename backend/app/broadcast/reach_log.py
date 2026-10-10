"""触达记录与失败原因分类（T3-14，技术方案 12.5 / 11.6 / 11.7 / 11.9）。

本模块是"每次触达尝试都落一条记录、失败必须归类"这条要求的唯一实现处，提供三件事：

1. `classify_fail`：把"是否可发送 / 是否消费到额度 / 是否抛异常"翻译成四类失败原因之一，
   成功返回 `None`。四类判据的优先级即调用顺序：时段最先、额度其次、接口异常最后。
2. `record_attempt`：成功与失败都必须落库，是"失败不得静默丢弃"（11.7 第三条）的落点。
3. `reach_metrics`：送达率与失败构成的统计口径，供 T6-09 的线上指标采集。

**`quiet_hours` 单列且不进"系统故障"口径**（本项相对 9.4 三类划分增加第四类的全部理由）：
静默时段（22:00~06:00，11.9）产生的延迟不是故障，若混入失败率会同时抬高失败率、误导运维去
排查不存在的接口问题、并让告警阈值无法设定。因此：

- `delivered_rate` 的分母用"已尝试且未被时段延迟"的条目数（`total - quiet_hours`），
  这样静默延迟不会把送达率拉低；技术方案 14.3 的"一次性订阅达成率"即此值。
- `quiet_hours_rate` 的分母是全部尝试数 `total`，用于回答"有多少消息因时段被延迟"。
- 三个真实失败率（`no_grant` / `quota_exhausted` / `api_error`）的分母同样是 `total`。

这条口径必须与 T6-09 的指标定义保持一致，避免两处算法漂移。
"""

import logging
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast.window import FARMER_TZ
from app.db.base import new_id
from app.models import ReachRecord
from app.models.enums import ReachStatus
from app.repositories.reach import reach_repo

logger = logging.getLogger(__name__)

# 触达记录的时间戳一律按农户时区落库：`attempted_at` 要能与静默窗口（11.9，按农户本地时区
# 判定）直接对照，用 UTC 会偏移 8 小时，使复盘时"这条为什么在半夜发"无法解释。
TZ = FARMER_TZ

# 四类失败原因的字面量，供 `classify_fail` 返回与 `reach_metrics` 取用。
FAIL_NO_GRANT = "no_grant"
FAIL_QUOTA_EXHAUSTED = "quota_exhausted"
FAIL_API_ERROR = "api_error"
FAIL_QUIET_HOURS = "quiet_hours"


def classify_fail(*, sendable: bool, consumed: bool, granted_total: int, exc: Exception | None) -> str | None:
    """失败分类：按判据优先级返回四类之一，成功返回 `None`。

    顺序是刻意的，与推送链路的门槛顺序一致（T3-12 第二节）：

    1. 时段不可发送 → `quiet_hours`（最早判定，优先级最高；此时还没走到额度与接口）。
    2. 未消费到额度 → `no_grant`（从未授权）或 `quota_exhausted`（授权已用完）。
    3. 消费到额度但抛异常 → `api_error`。
    4. 其余 → `None`（成功）。
    """
    if not sendable:
        return FAIL_QUIET_HOURS
    if not consumed:
        return FAIL_NO_GRANT if granted_total == 0 else FAIL_QUOTA_EXHAUSTED
    if exc is not None:
        return FAIL_API_ERROR
    return None


async def record_attempt(
    db: AsyncSession,
    *,
    user_id: str,
    prompt_item_id: str,
    channel: str,
    fail_reason: str | None,
    note: str | None = None,
) -> ReachRecord:
    """记录一次触达尝试：成功与失败都必须落库，不允许静默丢弃。

    `status` 由 `fail_reason` 派生：有失败原因即 `failed`，否则 `delivered`，并把 `delivered_at`
    与 `attempted_at` 取同一时刻。这条派生规则与 `ReachRecord` 的组合校验器一致——不可能出现
    "失败了但没写原因"或"成功却带了原因"的记录。

    `note`（微信返回的错误码原文）用于排障：`reach_record` 表当前没有 `note` 列，故只写日志
    而不落库，避免为观测字段引入迁移；业务判据完全不依赖它。
    """
    now = datetime.now(TZ)
    if fail_reason is not None:
        logger.info(
            "触达失败 record user=%s item=%s channel=%s reason=%s note=%s",
            user_id,
            prompt_item_id,
            channel,
            fail_reason,
            note,
        )
    record = ReachRecord(
        id=new_id("rr"),
        user_id=user_id,
        prompt_item_id=prompt_item_id,
        channel=channel,
        status=ReachStatus.FAILED.value if fail_reason else ReachStatus.DELIVERED.value,
        fail_reason=fail_reason,
        attempted_at=now,
        delivered_at=None if fail_reason else now,
    )
    await reach_repo.save(db, record)
    return record


async def reach_metrics(db: AsyncSession, since: date) -> dict[str, float]:
    """触达指标：送达率、失败构成、静默延迟占比（口径见模块注释）。

    `delivered_rate` 的分母是 `total - quiet_hours`；其余比率的分母是 `total`。分母为 0 时
    各比率取 0.0，避免除零。
    """
    delivered = await reach_repo.count_by_status(db, ReachStatus.DELIVERED.value, since)
    failed = await reach_repo.count_by_fail_reason(db, since)
    total = delivered + sum(failed.values())
    quiet_hours = failed.get(FAIL_QUIET_HOURS, 0)
    attempted_not_delayed = total - quiet_hours

    def _rate(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    return {
        "delivered_rate": _rate(delivered, attempted_not_delayed),
        "no_grant_rate": _rate(failed.get(FAIL_NO_GRANT, 0), total),
        "quota_exhausted_rate": _rate(failed.get(FAIL_QUOTA_EXHAUSTED, 0), total),
        "api_error_rate": _rate(failed.get(FAIL_API_ERROR, 0), total),
        "quiet_hours_rate": _rate(quiet_hours, total),
    }


__all__ = [
    "FAIL_API_ERROR",
    "FAIL_NO_GRANT",
    "FAIL_QUIET_HOURS",
    "FAIL_QUOTA_EXHAUSTED",
    "TZ",
    "classify_fail",
    "reach_metrics",
    "record_attempt",
]
