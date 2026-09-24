"""领域事件层：事务性发件箱的对外入口。

只导出 `emit`、`OutboxDispatcher` 与事件类型常量。迁移与排查直接引
`app.models.event_outbox` 里的状态常量，本模块不重复导出，避免同一组常量有
两个真相来源。
"""

from app.events.handlers import (
    EVENT_CONSULTATION_CLOSED,
    EVENT_FOLLOWUP_DUE,
    EVENT_PRESCRIPTION_CREATED,
    EVENT_REACH_FAILED,
    EVENT_SNAPSHOT_REQUESTED,
    EVENT_SUBSCRIPTION_CONSUMED,
    EVENT_TYPES,
    resolve_task,
)
from app.events.outbox import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    OutboxDispatcher,
    emit,
)

__all__ = [
    "BACKOFF_SECONDS",
    "EVENT_CONSULTATION_CLOSED",
    "EVENT_FOLLOWUP_DUE",
    "EVENT_PRESCRIPTION_CREATED",
    "EVENT_REACH_FAILED",
    "EVENT_SNAPSHOT_REQUESTED",
    "EVENT_SUBSCRIPTION_CONSUMED",
    "EVENT_TYPES",
    "MAX_ATTEMPTS",
    "OutboxDispatcher",
    "emit",
    "resolve_task",
]
