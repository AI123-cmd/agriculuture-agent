"""结构化日志入口（T6-08）。

本阶段只提供"能用的日志对象"：各智能体与节点通过 `log.info(...)` / `log.warning(...)`
写结构化字段，而不是把变量拼进字符串——字段化之后才能按 `trace_id`、`agent` 过滤与
对账，这是后续排障的基本前提。

T6-08 补齐的部分：`configure_logging(settings)` 的处理器链（`contextvars` 合并、
trace 上下文注入、`TimeStamper`、`ObservabilityMasker` 脱敏、JSON/console 渲染器）、
以及"脱敏处理器必须排在渲染器之前"这条顺序约束。本模块先不落那条链，是因为它依赖
`Settings.LOG_LEVEL` / `LOG_FORMAT` 与脱敏组件，二者同属 T6-08。
"""

from typing import Any

import structlog

# 懒加载代理：首次调用时才按当时的 structlog 配置构造具体 logger，因此本模块可以在
# `configure_logging` 之前被导入（测试与被导入的节点都会在导入期取到它）。
log: Any = structlog.get_logger("app")

__all__ = ["log"]
