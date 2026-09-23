# T1-13 event_outbox 事务性发件箱

## 这一项要做什么

建立 `event_outbox` 表与独立投递进程，实现事务性发件箱模式：业务事务内先把领域事件写进 `event_outbox`，由独立进程读取并投入 ARQ 队列，投递成功后标记。这样避免"业务事务已提交但事件丢失"造成的数据不一致，落实技术方案 6.6 的事件可靠性要求。发件箱同时是"触达必须晚于快照"这条顺序约束的技术保证：`prescription.created`、`followup.due` 这类事件先落库再投递，消费者读到事件时数据必定已提交。

## 前置依赖

- T1-02 提供异步引擎、`AsyncSession` 与 PostgreSQL；T1-04 提供真实迁移链路，`event_outbox` 必须由迁移建立；T1-09 提供 `unit_of_work` 事务边界，发件箱写入必须与业务写入同一事务；T1-01 提供 `arq>=0.26.0` 与 Redis。
- 外部环境：Redis 7+ 作为队列后端，投递进程与 api 进程分开部署（技术方案 15.1 的 `worker` 容器）；需人工确认退避上限（本项按固定表退避，5 次后转 `dead`，超出由人工处理）。

## 参考章节

- 技术方案 第6章 6.6 事件驱动协同（六个事件表、异步化三个理由、事件可靠性）；第11章 11.8 推送达成的顺序约束（推送内容必须来自已落库快照）
- 技术方案 第12章 12.1 表清单总览（`event_outbox` 为新增表）；第15章 15.1 部署拓扑与第2章 2.1 现有技术栈盘点（Redis 提升为核心依赖）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/alembic/versions/0003_event_outbox.py` | 建表、状态索引、部分索引 |
| 新增 | `backend/app/models/event_outbox.py` | ORM 模型 `EventOutbox` |
| 新增 | `backend/app/events/__init__.py` | 导出 `emit` 与事件类型常量 |
| 新增 | `backend/app/events/outbox.py` | `emit()` 与 `OutboxDispatcher` |
| 新增 | `backend/app/events/handlers.py` | 事件类型到 ARQ 任务名的注册表 |
| 新增 | `backend/app/worker.py` | ARQ worker 入口：定时投递与任务注册 |
| 修改 | `backend/app/models/__init__.py`、`backend/app/services/ai_service.py` | 注册 `EventOutbox`；处方生成后同事务 `emit("prescription.created")` |
| 新增 | `backend/tests/test_outbox.py`、`test_outbox_dispatch.py` | 事务一致性、退避、幂等、并发抢占、端到端投递 |

## 实现要点

### 一、六个事件与生产者消费者

技术方案 6.6 的六个事件，本项只建通道与注册表，消费者在后续阶段落地：

| 事件 | 生产者 | 消费者 | 落地任务 |
| --- | --- | --- | --- |
| `prescription.created` | 处方节点（门禁通过后） | 回访、台账、评估服务 | T3-09、T3-15、T6-09 |
| `consultation.closed` | 主图结束 | 评估、画像服务 | T6-09、T2-08 |
| `snapshot.requested` | 小程序「更新提示」 | 快照生成服务 | T3-08 |
| `followup.due` | 定时任务 | 触达、快照服务 | T3-12、T3-08 |
| `subscription.consumed` | 触达服务 | 额度服务 | T3-11 |
| `reach.failed` | 触达服务 | 指标服务 | T3-14 |

`handlers.py` 把六个键注册到 ARQ 任务名，未实现的消费者指向占位任务（仅记日志），保证事件不丢且可观测。异步化的三个理由在此各有对应：其一，对话链路延迟预算紧张，回访登记、台账写入、评测样本留存都移出请求，`emit` 只是插入一行，毫秒级，不影响首字响应；其二，触达必须与生成解耦，产品说明书 4.3 功能点 7 要求推送内容必须是快照里已存在的条目，`snapshot.requested` 与快照写入同事务，消费者读到事件时快照必已落库；其三，失败隔离，产品说明书 6.3 兜底五要求推送失败不影响任何已有功能，异步加队列天然满足。

### 二、表结构与索引

```python
class EventOutbox(Base):
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

`status` 取 `pending`/`dispatched`/`dead` 三值而非布尔，因为要表达"重试耗尽需人工介入"的第三态，`dead` 必须可查否则失败被静默吞掉。`idempotency_key` 唯一索引，值由事件类型加聚合根 ID 拼成（如 `prescription.created:card_abc123`），作用有三：重复 `emit` 被唯一约束挡住、给消费者做幂等依据、让"同一处方多次发事件"这类 bug 在写入阶段暴露。`payload` 用 `JSONB` 而非 `JSON`，因为要按 `payload->>'process_id'` 查询统计；`available_at` 与 `attempts` 支持退避，投递查询条件为 `status='pending' AND available_at <= now()`。

`(status, available_at)` 复合索引是投递查询的主索引，也是唯一的高频查询；`aggregate_id`/`user_id` 索引用于排查统计。再加一条部分索引 `CREATE INDEX ix_outbox_pending ON event_outbox (available_at) WHERE status = 'pending';`——它只含 `pending` 行，已投递的 `dispatched` 行不占这份索引，索引大小与待投递量成正比而非与历史总量成正比。

### 三、emit 骨架

```python
# app/events/outbox.py
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import EventOutbox

async def emit(db: AsyncSession, *, event_type: str, aggregate_type: str,
               aggregate_id: str, user_id: str | None, payload: dict,
               idempotency_key: str | None = None) -> EventOutbox:
    """在当前事务内写入待投递事件；不 commit，提交由 unit_of_work 负责。
    业务回滚则事件一并回滚，这是"事件与业务同生共死"的实现。"""
    row = EventOutbox(
        id=uuid.uuid4().hex, event_type=event_type, aggregate_type=aggregate_type,
        aggregate_id=aggregate_id, user_id=user_id, payload=payload,
        idempotency_key=idempotency_key or f"{event_type}:{aggregate_id}", status="pending")
    db.add(row)
    return row
```

关键点是 `emit` 不提交也不 flush，只 `db.add`。业务失败则事务回滚、事件一并消失，不会"有事件无数据"；业务成功则事件随提交落库。这是发件箱优于"事务提交后调 `enqueue`"的地方：后者在提交与入队之间失败时事件会丢。

### 四、投递进程

```python
# app/events/outbox.py（续）
import logging
from datetime import UTC, datetime, timedelta
from sqlalchemy import func, select, update
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
        async with SessionLocal() as db:
            rows = await self._claim(db, self.batch_size)
            if not rows:
                return 0
            await db.commit()                 # 提交抢占，缩短持锁时间
            for row in rows:
                await self._deliver(db, row)
            return len(rows)

    async def _claim(self, db: AsyncSession, limit: int) -> list[EventOutbox]:
        """FOR UPDATE SKIP LOCKED 抢占，多 worker 并行不阻塞、不重复。"""
        stmt = (select(EventOutbox)
                .where(EventOutbox.status == "pending", EventOutbox.available_at <= func.now())
                .order_by(EventOutbox.available_at).limit(limit).with_for_update(skip_locked=True))
        return list((await db.execute(stmt)).scalars().all())

    async def _deliver(self, db: AsyncSession, row: EventOutbox) -> None:
        try:
            await enqueue(resolve_task(row.event_type), row.payload, event_id=row.id)
            await db.execute(update(EventOutbox).where(EventOutbox.id == row.id).values(
                status="dispatched", dispatched_at=func.now(), attempts=row.attempts + 1))
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            row.attempts += 1
            row.last_error = str(exc)[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "dead"
            else:
                row.available_at = datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[row.attempts])
            await db.commit()
            logger.warning("事件投递失败 event=%s attempts=%s", row.id, row.attempts)
```

四个关键机制。其一，`FOR UPDATE SKIP LOCKED` 是多 worker 并行的正确做法：worker A 锁定 100 行时 worker B 跳过这些行去锁后面的，不阻塞不重复；普通 `FOR UPDATE` 会让第二个 worker 阻塞等待，不加锁则会重复投递（PostgreSQL 自 9.5 起支持）。其二，抢占后立即提交会释放行锁，留下一个窗口：若进程在提交抢占后、投递前崩溃，该行仍为 `pending`，下一轮会重新投递。这是可接受的，因为消费者按 `idempotency_key`/`event_id` 幂等；若要做"分区独占"需抢占时置 `delivering` 并提交，代价是崩溃后需超时回收卡住的行，本项选"至少一次投递 + 消费者幂等"，故障面更小。其三，退避表用常量而非公式，`(5, 30, 120, 600, 1800)` 让重试节奏可预测可调参，纯指数公式在多次重试后会溢出到不可能的时间。其四，失败不做分类，`last_error` 保留原文（截断 1000 字符）供死信排查，真正的分类在 T3-14 的 `reach_record.fail_reason` 做。

### 五、ARQ worker 入口

```python
# app/worker.py
from arq import cron
from arq.connections import RedisSettings
from app.core.config import get_settings
from app.events.outbox import OutboxDispatcher

async def dispatch_outbox(ctx) -> int:
    return await OutboxDispatcher().dispatch_once()

class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    functions = [dispatch_outbox]
    cron_jobs = [cron(dispatch_outbox, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55})]
```

ARQ 的 `cron` 最小粒度是秒级，用 `second={0,5,...}` 表达 5 秒一次。这与全局约定"任务队列用 ARQ"一致，也符合技术方案 4.5 明确不引入 Celery 的判断（避免同步 worker 与 async 应用的桥接复杂度）。投递延迟上限 5 秒，对回访与触达足够；对延迟敏感的 `snapshot.requested`（农户点「更新提示」等结果）走同步路径直接生成，不经过发件箱——发件箱用于"可以等 5 秒"的事件，不用于"用户正在等结果"的请求，这条区分要写进代码注释。

### 六、与验收项的对应

- 对应技术方案 6.6 的六个事件与事件可靠性要求；对应第四步之前的异步需求（T3-09 回访调度、T3-12 触达、T3-14 触达记录）。为产品说明书 9.2 第 14 条（主动触达通道）提供底座：触达记录必须来自快照，而快照与 `snapshot.requested` 同事务，顺序不可能违反。
- 为产品说明书 6.3 兜底五（推送失败不影响已有功能）提供实现，投递失败只影响该行 `status`，不影响任何业务接口；并为产品说明书 9.2 第 6 条（提示幂等与条数控制）提供幂等键机制。

## 验收标准

- `alembic upgrade head` 后查 `information_schema.columns`，`event_outbox` 含 `id`、`event_type`、`aggregate_id`、`idempotency_key`、`status`、`attempts`、`available_at`、`dispatched_at` 等列；`SELECT indexdef FROM pg_indexes WHERE tablename='event_outbox'` 含一条带 `WHERE (status = 'pending'::text)` 的部分索引；连续两次插入相同 `idempotency_key` 时第二条抛 `IntegrityError`。
- 事务一致性：在 `unit_of_work` 内 `emit` 后抛异常，`SELECT count(*) FROM event_outbox` 不变；正常提交后增加 1。投递端到端：写入一条 `snapshot.requested` 后启动 worker，5 秒内该行 `status='dispatched'` 且 `dispatched_at` 非空。
- 退避与死信：让投递目标抛异常，连续 3 轮后该行 `attempts=3`、`status='pending'` 且 `available_at` 逐次推后；连续 5 轮后 `status='dead'` 且 `last_error` 非空。并发不重复：同时启动两个 `OutboxDispatcher` 处理 200 条待投递事件，断言每条事件 `attempts` 恰好为 1，总处理条数为 200。
- `pytest backend/tests/test_outbox.py` 与 `test_outbox_dispatch.py` 全部通过，合计不少于 10 条断言；`pytest -q` 全量通过，且 `grep -rn "asyncio.create_task" backend/app/services` 无输出。
- `ruff check backend/app`、`mypy backend/app`、`python backend/scripts/check_layers.py` 均通过；L4 规则对新增 `app/events/outbox.py` 不报违规（该模块只 `add` 发件箱表，不构成对业务表的裸查询）。
