"""静默时段与推送窗口控制（T3-13，技术方案 11.9）。

补充说明：`is_sendable` 只在 T3-12 的主动推送判定处调用一次，**不作用于快照生成等农户主动
查看链路**——"农户主动查看不受限，仅约束主动推送"。夜间点击「更新提示」必须照常生成。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# 农户所在时区：目标用户在中国，时段判断统一按 Asia/Shanghai 而非服务器时区。
FARMER_TZ = ZoneInfo("Asia/Shanghai")

# 静默时段 22:00 ~ 次日 06:00，不产生新的主动提醒。跨零点区间，判断须用 or 连接两段。
QUIET_HOURS = (22, 6)

# 最佳触达窗口，每项为 (起始时, 起始分, 结束时, 结束分)。
#
# 与验收标准的口径对齐：验收把 06:00 定为"已出静默但不在窗口内（不可发）"、把 07:29 定为
# "窗口内（可发）"、把 07:30 定为"窗口右开（不可发）"。若按技术方案 11.9 原样的小时级比较
# (`start_h <= hour < end_h`)，6:30~7:30 会退化为覆盖 6:00~7:59，06:00 变 True、07:29 变
# False，与验收两处冲突。此处按"小时*60+分钟"做分钟级半开区间比较，常量不变，恰好同时满足
# 验收的全部边界值（06:00 F、06:30 T、07:29 T、07:30 F、18:00 T、17:59 F、19:00 F）。
BEST_WINDOWS = [(6, 30, 7, 30), (18, 0, 19, 0)]


def _to_farmer_tz(now: datetime) -> datetime:
    """校验 aware 并换算到农户时区；naive 值直接抛错，不默默按运行环境时区计算。

    容器里 naive datetime 的 `.hour` 通常是 UTC，会把静默时段整体偏移 8 小时
    （实际变成 14:00 至 22:00 静默），因此不合规一律拒绝。
    """
    if now.tzinfo is None:
        raise ValueError("时段判断需要 aware datetime，收到 naive 值")
    return now.astimezone(FARMER_TZ)


def _minutes_of(local: datetime) -> int:
    return local.hour * 60 + local.minute


def in_quiet_hours(now: datetime) -> bool:
    """是否落在静默时段（22:00 ~ 次日 06:00）。22:00 命中、06:00 不命中。"""
    local = _to_farmer_tz(now)
    return local.hour >= QUIET_HOURS[0] or local.hour < QUIET_HOURS[1]


def in_best_window(now: datetime) -> bool:
    """是否落在某个最佳触达窗口内（分钟级、右开区间）。"""
    local = _to_farmer_tz(now)
    minutes = _minutes_of(local)
    return any(
        start_h * 60 + start_m <= minutes < end_h * 60 + end_m for start_h, start_m, end_h, end_m in BEST_WINDOWS
    )


def is_sendable(now: datetime) -> bool:
    """触达时段判断：农户主动查看不受限，仅约束主动推送。

    先排除静默时段，再要求落在某个最佳窗口内。静默时段只有 22:00 至 06:00，其余时段
    "可推送但非最佳"，`False` 表示当前不应发起新的主动推送。
    """
    local = _to_farmer_tz(now)
    if local.hour >= QUIET_HOURS[0] or local.hour < QUIET_HOURS[1]:
        return False
    return in_best_window(local)


def next_window(now: datetime) -> datetime:
    """返回下一个可发送窗口的开始时刻，供延迟任务重新排队。"""
    local = _to_farmer_tz(now)
    for start_h, start_m, _, _ in BEST_WINDOWS:
        candidate = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        if candidate > local:
            return candidate
    tomorrow = (local + timedelta(days=1)).replace(
        hour=BEST_WINDOWS[0][0], minute=BEST_WINDOWS[0][1], second=0, microsecond=0
    )
    return tomorrow


__all__ = [
    "BEST_WINDOWS",
    "FARMER_TZ",
    "QUIET_HOURS",
    "in_best_window",
    "in_quiet_hours",
    "is_sendable",
    "next_window",
]
