"""T3-13 验收：静默时段与推送窗口控制。

覆盖验收标准：静默边界（22:00/06:00）、最佳窗口边界（06:30/07:30/18:00 等）、晚间"可用但
非最佳"、跨日、时区（naive 抛错与 UTC 换算）、`next_window` 三个起点，以及与快照生成链路
的隔离（`is_sendable` 不得出现在 `snapshot.py`）。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.broadcast.window import (
    BEST_WINDOWS,
    FARMER_TZ,
    QUIET_HOURS,
    in_best_window,
    in_quiet_hours,
    is_sendable,
    next_window,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = datetime(2026, 5, 1, tzinfo=SHANGHAI)


def _local(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


# ---- 一、常量形状 ----


def test_quiet_hours_is_cross_midnight_pair() -> None:
    """静默时段是跨零点区间，用 (22, 6) 表达，不能写成单段的 22 <= h < 6。"""
    assert QUIET_HOURS == (22, 6)


def test_best_windows_are_four_tuples() -> None:
    """每个最佳窗口是 (起始时, 起始分, 结束时, 结束分) 四元组。"""
    assert BEST_WINDOWS == [(6, 30, 7, 30), (18, 0, 19, 0)]
    assert all(len(window) == 4 for window in BEST_WINDOWS)


# ---- 二、静默与窗口边界（验收标准第一段） ----


@pytest.mark.parametrize(
    ("hour", "minute"),
    [(22, 0), (21, 59), (6, 0), (7, 30), (17, 59), (23, 59)],
)
def test_is_sendable_false_at_boundaries(hour: int, minute: int) -> None:
    assert is_sendable(_local(hour, minute)) is False


@pytest.mark.parametrize(
    ("hour", "minute"),
    [(6, 30), (7, 29), (18, 0)],
)
def test_is_sendable_true_inside_best_windows(hour: int, minute: int) -> None:
    assert is_sendable(_local(hour, minute)) is True


# ---- 三、晚间"可用但非最佳"（刻意允许晚间推送） ----


@pytest.mark.parametrize("hour", [19, 20])
def test_evening_is_optional_not_best_but_not_quiet(hour: int) -> None:
    """19:00~22:00 不在最佳窗口内（is_sendable False），但也不是静默时段。"""
    now = _local(hour)
    assert is_sendable(now) is False
    assert in_quiet_hours(now) is False


def test_quiet_boundary_2159_vs_2200() -> None:
    """21:59 未被判为静默，22:00 被判为静默。"""
    assert in_quiet_hours(_local(21, 59)) is False
    assert in_quiet_hours(_local(22, 0)) is True


# ---- 四、跨日 ----


@pytest.mark.parametrize(
    ("hour", "minute"),
    [(0, 30), (3, 0), (5, 59)],
)
def test_cross_midnight_is_quiet(hour: int, minute: int) -> None:
    assert is_sendable(_local(hour, minute)) is False


def test_first_window_after_midnight_is_sendable() -> None:
    """跨日后的第一个窗口 06:30 可发送。"""
    assert is_sendable(_local(6, 30)) is True


def test_0600_out_of_quiet_but_not_in_window() -> None:
    """06:00 已出静默，但不在窗口内 → 不可发送。"""
    now = _local(6, 0)
    assert in_quiet_hours(now) is False
    assert is_sendable(now) is False


# ---- 五、时区 ----


def test_naive_datetime_raises() -> None:
    """naive datetime 的 .hour 依赖运行环境时区，必须直接抛错而非默默按本地算。"""
    with pytest.raises(ValueError):
        is_sendable(datetime(2026, 5, 1, 6, 30))


def test_utc_2230_is_beijing_0630_next_day_sendable() -> None:
    """UTC 22:30 对应北京时间次日 06:30（窗口内）→ 可发送。"""
    now = datetime(2026, 5, 1, 22, 30, tzinfo=timezone.utc)
    assert now.astimezone(SHANGHAI).hour == 6
    assert is_sendable(now) is True


def test_utc_2200_is_beijing_0600_next_day_not_sendable() -> None:
    """UTC 22:00 对应北京时间次日 06:00：已出静默但不在窗口 → 不可发送。"""
    now = datetime(2026, 5, 1, 22, 0, tzinfo=timezone.utc)
    assert now.astimezone(SHANGHAI).hour == 6
    assert in_quiet_hours(now) is False
    assert is_sendable(now) is False


def test_farmer_tz_is_shanghai() -> None:
    assert FARMER_TZ.key == "Asia/Shanghai"


# ---- 六、next_window ----


def test_next_window_from_2300_is_next_day_0630() -> None:
    assert next_window(_local(23, 0)) == _local(6, 30) + timedelta(days=1)


def test_next_window_from_0700_is_same_day_1800() -> None:
    assert next_window(_local(7, 0)) == _local(18, 0)


def test_next_window_from_1900_is_next_day_0630() -> None:
    assert next_window(_local(19, 0)) == _local(6, 30) + timedelta(days=1)


def test_next_window_from_0630_returns_1800_same_day() -> None:
    """恰好在窗口开始时刻：candidate 不晚于 now，返回当日傍晚窗口。"""
    assert next_window(_local(6, 30)) == _local(18, 0)


def test_next_window_returns_farmer_tz_aware() -> None:
    result = next_window(_local(19, 0))
    assert result.tzinfo is not None
    assert result.utcoffset() == _local(19, 0).utcoffset()


# ---- 七、in_best_window ----


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (6, 0, False),
        (6, 29, False),
        (6, 30, True),
        (7, 29, True),
        (7, 30, False),
        (17, 59, False),
        (18, 0, True),
        (18, 59, True),
        (19, 0, False),
    ],
)
def test_in_best_window_minute_precise(hour: int, minute: int, expected: bool) -> None:
    assert in_best_window(_local(hour, minute)) is expected


# ---- 八、与生成链路隔离 ----


def test_is_sendable_absent_from_snapshot_generation() -> None:
    """农户主动查看不受限：快照生成链路不得出现 is_sendable。"""
    snapshot_py = Path(__file__).resolve().parents[2] / "app" / "broadcast" / "snapshot.py"
    assert "is_sendable" not in snapshot_py.read_text(encoding="utf-8")
