"""可观测性子包：结构化日志、指标采集与链路追踪的统一入口。

业务代码只从这里取 `log`、`metrics` 与追踪包装器，不直接使用标准库 `logging` 与 `print`：
统一的入口是"日志与 trace 只经一个序列化器落盘"（脱敏）与"指标口径只有一处"能够成立的
前提。脱敏处理器（T6-08）与指标落库（T6-09）都挂在这两个入口上。

`traced_node` / `configure_tracing` / `current_trace_context` 由 T6-06 提供，节点与网关
经它们写 span，不在各自模块里拼接属性键。
"""

from .logging import bind_context, bound_context, clear_context, configure_logging, log
from .masker import ObservabilityMasker
from .metrics import (
    MetricsCollector,
    checkpoint_write_samples,
    drain_checkpoint_write_samples,
    drain_node_duration_groups,
    metrics,
    node_duration_groups,
    observe_checkpoint_write_ms,
    observe_node_duration,
    over_round_rate,
    should_rollback_prompt,
)
from .prompts import record_prompt_identity
from .tracing import (
    GENERATION_SPAN_NAME,
    ROOT_SPAN_NAME,
    configure_tracing,
    consultation_span,
    current_trace_context,
    force_flush,
    generation_span,
    record_generation,
    reset_tracing,
    traced_node,
)

__all__ = [
    "GENERATION_SPAN_NAME",
    "MetricsCollector",
    "ObservabilityMasker",
    "ROOT_SPAN_NAME",
    "bind_context",
    "bound_context",
    "checkpoint_write_samples",
    "clear_context",
    "configure_logging",
    "configure_tracing",
    "consultation_span",
    "current_trace_context",
    "drain_checkpoint_write_samples",
    "drain_node_duration_groups",
    "force_flush",
    "generation_span",
    "log",
    "metrics",
    "node_duration_groups",
    "observe_checkpoint_write_ms",
    "observe_node_duration",
    "over_round_rate",
    "record_generation",
    "record_prompt_identity",
    "reset_tracing",
    "should_rollback_prompt",
    "traced_node",
]
