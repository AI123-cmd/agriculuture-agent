"""订阅授权与额度服务（T3-11，技术方案 11.7）。

把"每一次触达都当作一次需要争取的机会"这条产品判断落成一套可核算的额度账本。额度模型的
定位来自产品说明书 4.6.2 功能点 3：长期订阅消息对农业类小程序默认不可开通，因此**不以
"建立长期推送关系"为前提**，每次主动触达都要先有农户的一次授权、再消费掉这次授权。

四条硬规则（11.7）在本模块的落点：

| 规则 | 本模块的实现 |
| --- | --- |
| 授权一次、消费一次 | `grant` 加一份、`try_consume` 原子扣一份、`refund` 在发送失败时退还一份 |
| 无额度不得调用推送接口 | `try_consume` 返回 `False` 即额度不足，调用方（T3-12）据此记 `quota_exhausted` 并**不调用**微信接口 |
| 失败不得静默丢弃 | 本模块只负责账本；失败落库由 T3-12 / T3-14 写 `reach_record`，但 `refund` 保证"没送达就不算消费" |
| 长期无额度不反复索要 | `should_weaken_prompt` 判定授权入口是否降级为弱提示 |

**授权的记录与退还**：`grant` 记录前端回传的授权结果——`granted=True` 时 `granted_total`
与 `remaining` 各加一并清零 `reject_streak`，`granted=False` 时只 `record_reject`。两处都返回
最新额度视图，使前端不必再发一次查询。`refund` 供 T3-12 在"扣减成功但微信接口失败"时调用，
把消费退回——"授权一次消费一次"消费的是**成功送达**而非**尝试发送**（技术方案 11.7 第三条）。

**降级索要的判据**（11.7 第四条）：技术方案原文的 `should_weaken_prompt` 除 `reject_streak`
外还有一条 `_days_since_first_seen(quota) >= WEAKEN_AFTER_DAYS`，但 T3-01 冻结的
`subscription_quota` 表**没有"首次出现时间"列**（本项改动范围也不含迁移）。之所以能安全省略：
`reject_streak` 由每日任务在"无额度且无新增授权"时递增（技术方案 11.7 第四条对拒绝记录方式的
说明），因此"连续 N 天无额度"本就等价于 `reject_streak >= N`，两条判据退化为同一条，不需要
再引入一个没有数据来源的时间戳。降级不是永久放弃：农户下次生成处方卡或登记回访时（产品说明书
8.7 的三个高频意愿时点）仍会在那些位置请求授权，那时是新场景而非重复索要。

额度是每户的资产：`user_id` 只来自令牌（路由层 `get_current_user`），本服务不接受请求体传入，
见 `app/api/v1/subscription.py`。
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SubscriptionQuota
from app.repositories.quota import quota_repo
from app.schemas.subscription import GrantRequest, QuotaView

# 连续 N 天无额度则授权入口降级为弱提示（技术方案 11.7 第四条）。
# 取 3：农户授权意愿与具体场景强相关（刚生成处方卡时意愿最强），连续三天都不授权说明当前不是
# 合适时机，继续在同位置索要会变成骚扰。
WEAKEN_AFTER_DAYS = 3


def should_weaken_prompt(quota: SubscriptionQuota, today: date) -> bool:
    """是否把授权入口降级为弱提示（弱提示 = 只在提示列表底部保留一行不打扰的说明）。

    两段判定，顺序即优先级：

    1. 尚有额度（`remaining > 0`）永远不降级——农户还能收到推送，没有需要索要的场景。
    2. 无额度且连续 `WEAKEN_AFTER_DAYS` 天无额度（`reject_streak`）则降级。沉默与明确拒绝都
       计入 `reject_streak`（见 `record_reject`），避免"农户不点任何按钮"被误判为仍有机会。
    """
    if quota.remaining > 0:
        return False
    return quota.reject_streak >= WEAKEN_AFTER_DAYS


def _to_view(quota: SubscriptionQuota | None, today: date) -> QuotaView:
    """把额度行投影成接口视图；从未有过交互（无行）视同全零账本、不降级。

    全零且不降级是刻意的：新农户尚未被索要过，`reject_streak=0` 不满足降级条件，界面应正常
    展示授权入口（这是"争取一次机会"的入口，不是"反复索要"）。
    """
    if quota is None:
        return QuotaView(remaining=0, grantedTotal=0, consumedTotal=0, weakened=False)
    return QuotaView(
        remaining=quota.remaining,
        grantedTotal=quota.granted_total,
        consumedTotal=quota.consumed_total,
        weakened=should_weaken_prompt(quota, today),
    )


async def get_quota(db: AsyncSession, user_id: str, *, today: date | None = None) -> QuotaView:
    """读某户额度视图（`remaining / grantedTotal / consumedTotal / weakened`）。"""
    quota = await quota_repo.get(db, user_id)
    return _to_view(quota, today or date.today())


async def grant(db: AsyncSession, user_id: str, req: GrantRequest, *, today: date | None = None) -> QuotaView:
    """记录一次授权结果并返回最新额度视图。

    `granted=True`：`granted_total` 与 `remaining` 各加一、`reject_streak` 清零。
    `granted=False`：只记一次拒绝（`reject_streak` 加一），额度不变。
    返回最新视图使前端不需要再发一次查询（技术方案 11.7 第三条）。
    """
    if req.granted:
        quota = await quota_repo.grant(db, user_id)
    else:
        quota = await quota_repo.record_reject(db, user_id)
    await db.commit()
    return _to_view(quota, today or date.today())


async def try_consume(db: AsyncSession, user_id: str) -> bool:
    """原子扣减一份额度，返回是否成功；失败即额度不足，调用方不得发送。

    提交在本函数内完成：T3-12 的顺序是"**先扣额度再发送**"，扣减必须在调用外部接口之前落库，
    否则并发下同一份额度会被两次发送双花（技术方案 11.7 第一条）。
    """
    ok = await quota_repo.try_consume(db, user_id)
    await db.commit()
    return ok


async def refund(db: AsyncSession, user_id: str) -> bool:
    """退还一份额度（发送失败时调用），返回是否确有可退记录。提交在本函数内完成。"""
    ok = await quota_repo.refund(db, user_id)
    await db.commit()
    return ok


__all__ = [
    "WEAKEN_AFTER_DAYS",
    "get_quota",
    "grant",
    "refund",
    "should_weaken_prompt",
    "try_consume",
]
