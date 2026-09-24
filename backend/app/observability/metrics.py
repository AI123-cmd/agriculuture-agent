"""指标采集（T6-09）。

本阶段只提供最朴素的进程内计数器与直方图：智能体需要上报"降级了多少次""重试了
多少次"，这些数据在排障时比日志更早暴露问题，因此采集点必须先于看板存在。

T6-09 补齐的部分：`over_round_rate` 等产品级指标口径、`observe_checkpoint_write_ms`
的检查点埋点、落库写入器与 `app/repositories/metrics.py` 的聚合查询。

标签（`tags`）在本实现里参与计数键：同一指标名下不同标签组合各计一份。因此标签值
必须可哈希且顺序无关——内部按键排序后转成元组做键，避免 `{"a": 1, "b": 2}` 与
`{"b": 2, "a": 1}` 被当成两个不同的桶。
"""

from collections.abc import Mapping
from typing import Any

Tags = Mapping[str, str]
_Key = tuple[str, tuple[tuple[str, str], ...]]


def _key(name: str, tags: Tags | None) -> _Key:
    return (name, tuple(sorted((tags or {}).items())))


class MetricsCollector:
    """进程内计数器与直方图。多进程部署时由 T6-09 的落库写入器汇总。"""

    def __init__(self) -> None:
        self._counters: dict[_Key, int] = {}
        self._histograms: dict[_Key, list[float]] = {}

    def incr(self, name: str, *, tags: Tags | None = None, value: int = 1) -> None:
        """计数器 +value。智能体降级、重试、闸门拦截这类"发生次数"都走这里。"""
        key = _key(name, tags)
        self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, value: float, *, tags: Tags | None = None) -> None:
        """直方图观测值。耗时类指标走这里，取值由 T6-09 的聚合查询算分位数。"""
        self._histograms.setdefault(_key(name, tags), []).append(value)

    def counter_value(self, name: str, *, tags: Tags | None = None) -> int:
        """读计数器当前值，供单测断言与调试。"""
        return self._counters.get(_key(name, tags), 0)

    def histogram_values(self, name: str, *, tags: Tags | None = None) -> list[float]:
        """读直方图观测值列表（副本，调用方修改不影响采集器内部状态）。"""
        return list(self._histograms.get(_key(name, tags), []))

    def reset(self) -> None:
        """清空全部指标。测试之间必须调用，否则用例相互污染。"""
        self._counters.clear()
        self._histograms.clear()

    def snapshot(self) -> dict[str, Any]:
        """导出当前全部指标，供排障接口或测试整体断言。"""
        return {
            "counters": {f"{name}{dict(tags) or ''}": value for (name, tags), value in self._counters.items()},
            "histograms": {f"{name}{dict(tags) or ''}": values for (name, tags), values in self._histograms.items()},
        }


# 全局采集器。各层直接用 `from app.observability import metrics` 上报，不传实例，
# 否则每个构造函数都要多一个参数，而指标上报是横切关注点。
metrics = MetricsCollector()

__all__ = ["MetricsCollector", "Tags", "metrics"]
