"""事件类型 → ARQ 任务名的注册表（技术方案 6.6 的六个事件）。

本模块只维护"哪个事件交给哪个任务"，不含任何投递或重试逻辑——那部分在
`app/events/outbox.py`。刻意把映射独立出来的理由：映射是**长期稳定**的契约
（技术方案 6.6 定死了六个事件），而消费者实现要等到 T3 阶段才逐个落地。把两者
分开后，后续补实现只需新增任务函数并在 `app/worker.py` 换掉注册，映射本身不动。

六个消费者的实现分别落在 T3-09（`prescription_created`）/ T3-12（`followup_due`）/
T3-14（`reach.failed` 消费，写指标占位接 T6-09）/ T3-08（`snapshot_requested`）/
T2-08（`consultation_closed`）；`subscription.consumed` 的额度消费者尚未开工。未开工的任务名由
`app/worker.py` 注册到占位实现：事件照常入队、照常有日志，不丢也不静默——`event_outbox` 里
`dispatched` 的行数因此可以作为"事件已送达队列"的真实指标。本模块只维护映射，不因消费者落地而改动。
"""

# 六个事件类型（技术方案 6.6）。用常量而非裸字符串：拼写错误在导入期就暴露，
# 而裸字符串只会让事件静静地进不了注册表。
EVENT_PRESCRIPTION_CREATED = "prescription.created"
EVENT_CONSULTATION_CLOSED = "consultation.closed"
EVENT_SNAPSHOT_REQUESTED = "snapshot.requested"
EVENT_FOLLOWUP_DUE = "followup.due"
EVENT_SUBSCRIPTION_CONSUMED = "subscription.consumed"
EVENT_REACH_FAILED = "reach.failed"

# 未知事件类型的兜底任务名。存在的意义是"可观测"：没有它，未注册的事件会在
# 投递阶段抛 KeyError、被当成投递失败而反复重试，最后变成死信却查不出原因；
# 有它则明确记一条日志，一眼可见"有个事件类型忘了注册"。
UNHANDLED_TASK = "outbox_unhandled"

# 事件类型 → ARQ 任务名。键集合必须与技术方案 6.6 的六个事件逐字一致。
_TASK_BY_EVENT: dict[str, str] = {
    EVENT_PRESCRIPTION_CREATED: "prescription_created",
    EVENT_CONSULTATION_CLOSED: "consultation_closed",
    EVENT_SNAPSHOT_REQUESTED: "snapshot_requested",
    EVENT_FOLLOWUP_DUE: "followup_due",
    EVENT_SUBSCRIPTION_CONSUMED: "subscription_consumed",
    EVENT_REACH_FAILED: "reach_failed",
}

EVENT_TYPES = frozenset(_TASK_BY_EVENT)


def resolve_task(event_type: str) -> str:
    """把事件类型解析成 ARQ 任务名；未知类型返回兜底任务而非抛异常。

    返回兜底任务而不是抛异常，是为了让"上游发了一个新事件类型"表现为一条可读的
    日志，而不是一条反复重试到死的死信。写错事件名的代价被压到最小。
    """
    return _TASK_BY_EVENT.get(event_type, UNHANDLED_TASK)


def registered_task_names() -> list[str]:
    """全部任务名（含兜底），供 worker 注册时遍历。

    做成函数而非模块级列表：注册表是唯一真相，worker 从它派生注册项，
    避免"加了事件却忘了在 worker 里注册"这类只在运行时才暴露的疏漏。
    """
    return sorted({*_TASK_BY_EVENT.values(), UNHANDLED_TASK})


__all__ = [
    "EVENT_CONSULTATION_CLOSED",
    "EVENT_FOLLOWUP_DUE",
    "EVENT_PRESCRIPTION_CREATED",
    "EVENT_REACH_FAILED",
    "EVENT_SNAPSHOT_REQUESTED",
    "EVENT_SUBSCRIPTION_CONSUMED",
    "EVENT_TYPES",
    "UNHANDLED_TASK",
    "registered_task_names",
    "resolve_task",
]
