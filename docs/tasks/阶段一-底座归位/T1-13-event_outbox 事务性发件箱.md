# T1-13 event_outbox 事务性发件箱

## 这一项要做什么

建立 `event_outbox` 表与投递进程，实现"事务性发件箱"模式：业务事务内先把领域事件写进 `event_outbox`，由独立的投递进程读取并投入 ARQ 队列，投递成功后标记。这样避免"业务事务已提交但事件丢失"造成的数据不一致，也是技术方案 6.6 要求的"事件可靠性"的落地方式。

发件箱同时是"触达必须晚于快照"这条顺序约束的技术保证：`prescription.created`、`followup.due` 这类事件先落库再投递，消费者才可能看到已完成落库的数据。

## 前置依赖

- 依赖 T1-02 已提供：异步引擎、`AsyncSession`、PostgreSQL。
- 依赖 T1-04 已提供：真实迁移链路。`event_outbox` 表必须通过迁移建立，不能靠启动建表。
- 依赖 T1-09 已提供：`unit_of_work` 事务边界，发件箱的写入必须与业务写入在同一事务内。
- 依赖 T1-01 已提供：`arq>=0.26.0` 与 Redis。
- 外部环境：Redis 7+ 作为队列后端；投递进程与 api 进程分开部署（技术方案 15.1 的 `worker` 容器）。
- 需要人工确认：投递失败的退避策略上限（本项按指数退避、上限 5 次后转 `dead`，超出后由人工处理）。

## 参考章节

- 技术方案 第6章 6.6 事件驱动协同（六个事件表、异步化三个理由、事件可靠性段落）
- 技术方案 第11章 11.8 推送达成的顺序约束（推送内容必须来自已落库快照）
- 技术方案 第12章 12.1 表清单总览（`event_outbox` 新增，用于异步可靠性）
- 技术方案 第15章 15.1 部署拓扑（`worker` 独立容器、`api` 与 `worker` 负载特征不同）
- 技术方案 第2章 2.1 现有技术栈盘点（Redis 提升为核心依赖，含任务队列）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/alembic/versions/0003_event_outbox.py` | 建表、状态索引、清理索引 |
| 新增 | `backend/app/models/event_outbox.py` | ORM 模型 `EventOutbox` |
| 新增 | `backend/app/events/__init__.py` | 事件包入口，导出 `emit` 与事件常量 |
| 新增 | `backend/app/events/outbox.py` | `emit()` / `OutboxDispatcher`，含退避与幂等 |
| 新增 | `backend/app/events/handlers.py` | 事件到 ARQ 任务的映射（消费者注册表） |
| 新增 | `backend/app/worker.py` | ARQ worker 入口：定时投递 + 任务注册 |
| 修改 | `backend/app/models/__init__.py` | 注册 `EventOutbox` |
| 修改 | `backend/app/services/ai_service.py` | 处方生成后在同一事务内 `emit("prescription.created")`（占位接入） |
| 新增 | `backend/tests/test_outbox.py` | 事务一致性、退避、幂等、并发抢占 |
| 新增 | `backend/tests/test_outbox_dispatch.py` | 投递进程端到端：写入 → 投递 → 标记 |

## 实现要点

### 一、六个事件与生产者消费者

技术方案 6.6 给出的六个事件，本项只建通道与注册表，具体消费者在后续阶段落地：

| 事件 | 生产者 | 消费者 | 落地任务 |
| --- | --- | --- | --- |
| `prescription.created` | 处方节点（门禁通过后） | 回访服务、台账服务、评估服务 | T3-09、T3-15、T6-09 |
| `consultation.closed` | 主图结束 | 评估服务、画像服务 | T6-09、T2-08 |
| `snapshot.requested` | 小程序「更新提示」 | 快照生成服务 | T3-08 |
| `followup.due` | 定时任务 | 触达服务、快照服务 | T3-12、T3-08 |
| `subscription.consumed` | 触达服务 | 额度服务 | T3-11 |
| `reach.failed` | 触达服务 | 指标服务 | T3-14 |

本项在 `handlers.py` 里注册这六个键到 ARQ 任务名，未实现的消费者指向占位任务（记录日志），保证事件不丢且可观测。

### 二、异步化的三个理由（为什么不让 api 进程直接干活）

技术方案 6.6 给的三条理由，在本项的实现里各有对应：

第一，对话链路的延迟预算紧张。农户在等回答，回访登记、台账写入、评测样本留存都移出请求。本项的 `emit` 是纯插入一行，耗时在毫秒级，不影响首字响应。

第二，触达必须与生成解耦。产品说明书 4.3 功能点 7 要求"推送内容必须是快照里已存在的条目，新生成的内容不得绕过快照直接推送"。事件驱动保证推送发生在快照落库之后：因为 `snapshot.requested` 事件与快照写入在同一事务，消费者读到事件时快照必定已提交。

第三，失败隔离。产品说明书 6.3 兜底五要求"推送失败不能影响任何已有功能"。异步 + 队列天然满足：触达服务失败只影响该条推送记录。

### 三、表结构

```python
class EventOutbox(Base):
    """事务性发件箱：业务事务内写，独立进程投递。"""

    __tablename__ = "event_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

字段设计逐项说明：

**`status` 三值 `pending` / `dispatched` / `dead`。** 不用布尔 `dispatched`，因为需要表达"重试次数用尽，需人工介入"这个第三态。`dead` 事件必须可查询，否则失败被静默吞掉。

**`idempotency_key` 唯一索引。** 值由"事件类型 + 聚合根 ID + 业务序号"拼成，例如 `prescription.created:card_abc123`。作用有三：一是重复 `emit` 同一事件会被唯一约束挡住（业务代码重试安全）；二是给消费者做幂等的依据；三是让"同一处方多次发事件"这类 bug 在写入阶段就暴露。

**`payload` 用 JSONB 而非 JSON。** PostgreSQL 的 `JSONB` 支持索引与去重，且本项目的 payload 需要被查询（按 `payload->>'process_id'` 统计）。全局约定要求全异步与 PG，此处用 PG 特性是合理的。

**`available_at` 与 `attempts` 支持指数退避。** 投递失败时 `attempts += 1`，`available_at = now() + backoff(attempts)`。投递查询条件为 `status='pending' AND available_at <= now()`。

**索引设计。** `(status, available_at)` 复合索引是投递查询的主索引——这是唯一的高频查询。`aggregate_id` 与 `user_id` 索引用于排查与统计。建议再加一条部分索引减少索引体积：

```sql
CREATE INDEX ix_outbox_pending ON event_outbox (available_at)
WHERE status = 'pending';
```

部分索引只含 `pending` 行。投递完成后 `dispatched` 行不占这份索引，索引大小与待投递量成正比，而不是与历史总量成正比。这是本项的一个关键性能设计。

### 四、emit 的骨架

```python
# app/events/outbox.py
"""事务性发件箱：写入与业务在同一事务，投递与业务解耦。"""
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventOutbox


async def emit(
    db: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    user_id: str | None,
    payload: dict,
    idempotency_key: str | None = None,
) -> EventOutbox:
    """在**当前事务**内写入一条待投递事件。

    不调用 commit：提交由调用方的 unit_of_work 负责。
    这正是"事件与业务同生共死"的实现方式——业务回滚，事件一并回滚。
    """
    row = EventOutbox(
        id=uuid.uuid4().hex,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        user_id=user_id,
        payload=payload,
        idempotency_key=idempotency_key or f"{event_type}:{aggregate_id}",
        status="pending",
    )
    db.add(row)
    return row
```

关键点：**`emit` 不提交也不 flush**。它只是 `db.add`。提交由外层的 `unit_of_work`（T1-09）完成。这样：

- 业务写入失败 → 事务回滚 → 事件一并消失，不会出现"有事件无数据"。
- 业务写入成功 → 事务提交 → 事件落库，投递进程才能看到。

这是发件箱模式的核心，也是它优于"事务提交后调 `enqueue`"的地方——后者在提交与入队之间失败时事件丢失。

### 五、投递进程

```python
# app/events/outbox.py（续）
import asyncio
import logging

from sqlalchemy import select, update

from app.db.session import SessionLocal
from app.events.handlers import resolve_task

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 30, 120, 600, 1800)


class OutboxDispatcher:
    """独立进程：批量抢占待投递事件，投入 ARQ 队列，标记结果。"""

    def __init__(self, batch_size: int = 100) -> None:
        self.batch_size = batch_size

    async def dispatch_once(self) -> int:
        """投递一轮，返回处理条数。"""
        async with SessionLocal() as db:
            rows = await self._claim(db, self.batch_size)
            if not rows:
                return 0
            await db.commit()                  # 先提交抢占，缩短持锁时间
            for row in rows:
                await self._deliver(db, row)
            return len(rows)

    async def _claim(self, db: AsyncSession, limit: int) -> list[EventOutbox]:
        """用 FOR UPDATE SKIP LOCKED 抢占，支持多 worker 并行不重复。"""
        stmt = (
            select(EventOutbox)
            .where(EventOutbox.status == "pending", EventOutbox.available_at <= func.now())
            .order_by(EventOutbox.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await db.execute(stmt)).scalars().all())

    async def _deliver(self, db: AsyncSession, row: EventOutbox) -> None:
        task = resolve_task(row.event_type)
        try:
            await enqueue(task, row.payload, event_id=row.id)
            await db.execute(
                update(EventOutbox)
                .where(EventOutbox.id == row.id)
                .values(status="dispatched", dispatched_at=func.now(), attempts=row.attempts + 1)
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            row.attempts += 1
            row.last_error = str(exc)[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "dead"
            else:
                row.available_at = datetime.now(UTC) + timedelta(
                    seconds=BACKOFF_SECONDS[row.attempts]
                )
            await db.commit()
            logger.warning("事件投递失败 event=%s attempts=%s", row.id, row.attempts)
```

四个关键机制：

**`FOR UPDATE SKIP LOCKED` 抢占。** 这是多 worker 并行的正确做法：worker A 锁定 100 行时，worker B 直接跳过这些行去锁定后面的，不阻塞、不重复投递。若用普通的 `SELECT ... FOR UPDATE`，第二个 worker 会阻塞等待；若不用锁，两个 worker 会投递同一事件两次。PostgreSQL 自 9.5 起支持该语法。

**抢占后立即提交。** 提交会释放行锁（`FOR UPDATE` 的锁在事务结束时释放），因此在 `_deliver` 阶段行锁已不在。这带来一个窗口：若进程在提交抢占后、投递前崩溃，该行仍是 `pending`，会被下一轮重新投递。这是可接受的——因为投递要求消费者幂等，而 `idempotency_key` 与 `event_id` 提供了幂等依据。若要做到"分区独占"，需在抢占时改为把状态置为 `delivering` 并提交，代价是崩溃后会留下 `delivering` 卡住的行，需要超时回收。本项选择"至少一次投递 + 消费者幂等"，因为它的故障面更小、恢复更简单。

**退避表是常量而非公式。** `BACKOFF_SECONDS = (5, 30, 120, 600, 1800)` 让重试节奏可预测、可调参。纯指数公式（`2 ** n`）在 20 次重试后会溢出到不可能的时间，且运维难以从配置推断出"某类失败要多久出结果"。

**五类失败的区分在 `last_error`。** 本项不做失败分类，但记录原文（截断 1000 字符）。真正的分类在 T3-14 的 `reach_record.fail_reason` 做。这里保留原文是为了"死信排查"。

### 六、ARQ worker 入口

```python
# app/worker.py
"""ARQ worker：定时投递发件箱 + 执行队列任务。"""
from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.events.outbox import OutboxDispatcher


async def dispatch_outbox(ctx) -> int:
    """每 5 秒投递一轮。"""
    return await OutboxDispatcher().dispatch_once()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    functions = [dispatch_outbox]
    cron_jobs = [cron(dispatch_outbox, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55})]
```

ARQ 的 `cron` 最小粒度是秒级，用 `second={0,5,...}` 表达 5 秒一次。这一点与全局约定"任务队列用 ARQ"一致，且 ARQ 与 async 栈一致（技术方案 4.5 明确不引入 Celery，正因为"避免同步 worker 与 async 应用的桥接复杂度"）。

投递延迟上限为 5 秒，对回访与触达这类场景足够；对延迟更敏感的 `snapshot.requested`（农户点"更新提示"等结果），走同步路径直接生成，不经过发件箱。这条区分要写进代码注释——发件箱用于"可以等 5 秒"的事件，不用于"用户正在等结果"的请求。

### 七、与验收项的对应

- 对应技术方案 6.6 的六个事件与事件可靠性要求。
- 为产品说明书 9.2 第 14 条（主动触达通道）提供底座：`reach_record` 必须来自快照，而快照与 `snapshot.requested` 事件同事务，顺序不可能违反。
- 为产品说明书 6.3 兜底五（推送失败不影响已有功能）提供实现：投递失败只影响该行 `status`，不影响任何业务接口。
- 为产品说明书 9.2 第 6 条（提示幂等与条数控制）提供幂等键机制。
- 对应第四步之前的所有异步需求（T3-09 回访调度、T3-12 触达、T3-14 触达记录）。

## 验收标准

- `alembic upgrade head` 后 `SELECT column_name FROM information_schema.columns WHERE table_name='event_outbox'` 含 `id`、`event_type`、`aggregate_id`、`idempotency_key`、`status`、`attempts`、`available_at`、`dispatched_at` 八列。
- `SELECT indexdef FROM pg_indexes WHERE tablename='event_outbox'` 含一条 `WHERE (status = 'pending'::text)` 的部分索引。
- 唯一约束生效：连续两次插入相同 `idempotency_key` 的第二条抛 `IntegrityError`。
- 事务一致性：在 `unit_of_work` 内 `emit` 后抛异常，`SELECT count(*) FROM event_outbox` 不变；正常提交后增加 1。
- 投递端到端：写入一条 `snapshot.requested` 事件后启动 worker，5 秒内该行 `status='dispatched'` 且 `dispatched_at` 非空。
- 退避生效：让投递目标抛异常，连续触发 3 轮后该行 `attempts=3`、`status='pending'`，且 `available_at` 递增（第 2 次比第 1 次晚）。
- 死信：连续触发 5 轮失败后 `status='dead'`，且 `last_error` 非空。
- 并发不重复：同时启动两个 `OutboxDispatcher` 实例处理 200 条待投递事件，断言每条事件的 `attempts` 恰好为 1（投递一次），总处理条数为 200。
- `pytest backend/tests/test_outbox.py` 与 `pytest backend/tests/test_outbox_dispatch.py` 全部通过，合计不少于 10 条断言。
- `pytest -q` 全量通过，且 `grep -rn "asyncio.create_task" backend/app/services` 无输出（证明没有用后台任务替代发件箱）。
- `ruff check backend/app`、`mypy backend/app` 通过。
- `python backend/scripts/check_layers.py` 的 L4 规则对新增的 `app/events/outbox.py` 不报违规（该模块通过仓储或直接 `add` 写发件箱表，不构成对业务表的裸查询）。
