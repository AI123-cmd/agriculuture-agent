"""可观测性子包：结构化日志与指标采集的统一入口。

业务代码只从这里取 `log` 与 `metrics`，不直接使用标准库 `logging` 与 `print`：
统一的入口是"日志与 trace 只经一个序列化器落盘"（脱敏）与"指标口径只有一处"
能够成立的前提。脱敏处理器（T6-08）与指标落库（T6-09）都挂在这两个入口上。
"""

from .logging import log
from .metrics import MetricsCollector, metrics

__all__ = ["MetricsCollector", "log", "metrics"]
