# T1-05 OwnedMixin 与 OwnedRepository 归属安全基类

## 这一项要做什么

建立"归属安全"的工程基座：所有业务表统一通过 `OwnedMixin` 携带 `user_id`，所有数据访问统一通过 `OwnedRepository` 并强制注入归属条件，缺少 `user_id` 参数时直接抛异常。这是产品说明书 9.2 第 1 条（AI 诊断的数据写入未校验归属，存在越权风险）在架构层的解法——仅靠代码评审无法长期维持，必须让"忘记加过滤"在运行时不可能通过。现有 42 处 `db.query(Model)` 散落在 `app/services/`、`app/tools/`、`app/broadcast/`，其中 `ai_service._persist` 按 `sessionId` 查会话时未校验归属，是越权的实际入口。本项只建基类与异常，不改调用点（T1-06 做全量整改）。

## 前置依赖

- 依赖 T1-02 已提供：异步 `AsyncSession`、`async_sessionmaker`、带时区的 `TimestampMixin`；依赖 T1-04 已提供真实迁移链路，使新增 `user_id` 索引与约束可追溯。
- 外部环境：PostgreSQL 16+。需人工确认为每张表是否需要外键到 `user`；`user` 表本身与 `process_config` / `knowledge_item` 等无归属表不继承 `OwnedMixin`。

## 参考章节

- 技术方案 第12章 12.3 用户标识与归属安全（`OwnedMixin` / `OwnedRepository` 原文与"无 user_id 则抛异常"）、12.1 表清单总览、12.4 问诊与处方卡、12.6 环境数据三表
- 技术方案 第15章 15.2 分层与依赖约束（第 4 条硬规则：任何模块不得直接 `db.query(Model)`）；第16章 16.2 第一步：底座归位（技术动作 4：归属校验统一）
- 产品说明书 9.2 第 1 条、9.4 外键约束

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/app/db/owned.py`、`backend/app/core/ownership.py` | `OwnedMixin`、`OwnedModel` 协议、`OwnershipError` / `MissingOwnerError` |
| 新增 | `backend/app/repositories/base.py`、`conversation.py`、`__init__.py` | `OwnedRepository` 泛型基类、`ConversationRepository` 与子表仓储、包入口 |
| 修改 | `backend/app/models/*.py` | 9 张业务表改继承 `OwnedMixin`，删除手写 `user_id` 列 |
| 新增 | `backend/tests/repositories/test_owned_repository.py`、`backend/tests/test_ownership_403.py` | 缺参抛异常、跨用户查不到、异常映射 403 而非 500 |

## 实现要点

### 一、OwnedMixin 与协议（`app/db/owned.py`）

```python
"""归属安全的模型混入：所有业务表统一带 user_id 字段。"""
from typing import Protocol
from sqlalchemy import String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class OwnedMixin:
    """归属字段混入。declared_attr 让每个子类生成独立列对象。"""
    @declared_attr
    def user_id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(36), nullable=False, index=True, comment="归属用户ID")


class OwnedModel(Protocol):
    """供 OwnedRepository 做类型约束的最小协议。"""
    id: Mapped[str]
    user_id: Mapped[str]
```

三个设计取舍。第一，`user_id` 取 `String(36)` 与全局约定主键长度一致（现状用 `String(32)`，T1-04 已在基线统一为 36，混入不能在长度上开倒车）。第二，用 `declared_attr` 而非普通 `mapped_column`，否则多个子类继承同一混入时 SQLAlchemy 会复用同一 `Column` 对象并报 "Column object already assigned to Table"。第三，`index=True` 写进混入，因为全局约定要求"所有业务表带 `user_id`，索引必建"——放在混入里新增表自动带上，不依赖执行者记得加。

### 二、OwnedRepository 完整骨架（`app/repositories/base.py`）

```python
"""归属安全仓储基类：所有查询强制注入归属条件。"""
from collections.abc import Sequence
from typing import Any, Generic, TypeVar
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.ownership import MissingOwnerError, OwnershipError
from app.db.owned import OwnedModel

ModelT = TypeVar("ModelT", bound=OwnedModel)


class OwnedRepository(Generic[ModelT]):
    """铁律：任何查询方法都必须显式接收 user_id；缺失即抛异常，不做降级。"""
    model: type[ModelT]

    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    def _require_user(self, user_id: str | None) -> str:
        """空串、None、非字符串一律拒绝，不做降级。"""
        if not user_id or not isinstance(user_id, str):
            raise MissingOwnerError(f"{self.model.__name__} 缺少归属参数 user_id，已拒绝执行")
        return user_id

    def _owned_query(self, user_id: str) -> Select:
        """构造带归属条件的 SELECT，禁止调用方自行拼 where。"""
        return select(self.model).where(self.model.user_id == self._require_user(user_id))

    async def get(self, db: AsyncSession, user_id: str, obj_id: str) -> ModelT | None:
        """归属不符返回 None，不区分"不存在"与"不属于你"。"""
        stmt = self._owned_query(user_id).where(self.model.id == obj_id)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def get_or_raise(self, db: AsyncSession, user_id: str, obj_id: str) -> ModelT:
        obj = await self.get(db, user_id, obj_id)
        if obj is None:
            raise OwnershipError("资源不存在或无权访问")
        return obj

    async def list(self, db: AsyncSession, user_id: str, *, limit: int = 20,
                   offset: int = 0, order_by=None) -> Sequence[ModelT]:
        stmt = self._owned_query(user_id)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        return (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    async def count(self, db: AsyncSession, user_id: str) -> int:
        stmt = select(func.count()).select_from(self.model).where(
            self.model.user_id == self._require_user(user_id))
        return (await db.execute(stmt)).scalar_one()

    async def add(self, db: AsyncSession, user_id: str, obj: ModelT) -> ModelT:
        """强制写入归属，禁止调用方传入他人的 user_id。"""
        uid = self._require_user(user_id)
        if getattr(obj, "user_id", None) not in (None, uid):
            raise OwnershipError("新增对象的 user_id 与当前用户不一致")
        obj.user_id = uid
        db.add(obj)
        return obj

    async def update_fields(self, db: AsyncSession, user_id: str, obj_id: str, **fields: Any) -> int:
        uid = self._require_user(user_id)
        stmt = update(self.model).where(
            self.model.id == obj_id, self.model.user_id == uid).values(**fields)
        return (await db.execute(stmt)).rowcount

    async def delete_all(self, db: AsyncSession, user_id: str, **conditions: Any) -> int:
        stmt = delete(self.model).where(self.model.user_id == self._require_user(user_id))
        for field, value in conditions.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        return (await db.execute(stmt)).rowcount

    async def delete(self, db: AsyncSession, user_id: str, obj_id: str) -> int:
        return await self.delete_all(db, user_id, id=obj_id)
```

"缺少 `user_id` 参数即抛异常"不在于调用方是否记得传，而在于 `_require_user` 与 `_owned_query` 是构造查询的唯一出口：每个查询方法第一步都过 `_require_user`，因此 `user_id=""`、`None`、`0` 三种"看起来像有参数"的情况都会被拒。技术方案 12.3 的原型是 `if not user_id: raise OwnershipError(...)`，本项抽成 `MissingOwnerError` 子类，让"忘记传参"（编程错误，应告警）与"归属不符"（业务结果，正常 403）在日志里可区分。`get` 返回 None 而 `get_or_raise` 才抛，是为了让列表场景能跳过、详情场景能 403；后者对"不存在"与"不属于你"返回同一错误，避免用 404/403 差异枚举他人 ID。基类不提供任何返回裸 `AsyncSession` 或 `_raw_query` 的逃生舱。

### 三、异常、模型侧改动与子表仓储

```python
# app/core/ownership.py
class OwnershipError(AppException):
    def __init__(self, message: str = "资源不存在或无权访问"):
        super().__init__(403, message)


class MissingOwnerError(OwnershipError):
    """编程错误：仓储调用未传 user_id，与业务 403 在日志中可区分。"""
    def __init__(self, message: str = "缺少归属参数，拒绝执行查询"):
        super().__init__(message)


# app/models/ai_conversation.py —— 模型侧只做"加继承、删手写列"
class AIConversation(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "ai_conversation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # user_id 由 OwnedMixin 提供，删除手写的 user_id 列
    title: Mapped[str] = mapped_column(String(128), nullable=False)


# app/repositories/conversation.py —— 子表 ai_message 经父会话间接归属
class ConversationRepository(OwnedRepository[AIConversation]):
    def __init__(self) -> None:
        super().__init__(AIConversation)

    async def delete_with_messages(self, db: AsyncSession, user_id: str, conv_id: str) -> int:
        """先校验归属，再删子表，最后删父表。"""
        conv = await self.get(db, user_id, conv_id)
        if conv is None:
            raise OwnershipError("会话不存在或无权访问")
        await db.execute(delete(AIMessage).where(AIMessage.conversation_id == conv_id))
        return await self.delete(db, user_id, conv_id)
```

现有 `main.py` 的 `app_exception_handler` 用 `exc.code` 同时当业务码与 HTTP 状态码，`OwnershipError` 取 403 即可复用，不用改 `main.py`；注意现状 `AppException` 默认 code 为 500，本项不要顺手改它，否则会牵动 T1-09 的事务边界。应继承 `OwnedMixin` 的 9 张表：`farmer_profile`、`crop`、`field`、`device`、`location`、`broadcast_log`、`ai_conversation`、`memory_summary`、`user_profile_tag`。不继承的：`user`（归属主体自身）、`ai_message`（无 `user_id` 列，经 `conversation_id` 间接归属）、未来新增的全局数据表。`ai_message` 因此无法进 `OwnedRepository`，本项给它 `ConversationScopedRepository`：先校验父会话归属再操作消息，这是对技术方案的一处推断，方案未涉及子表形态。`delete_with_messages` 修的是现状缺陷：`ai_service.delete_history` 删除 `AIMessage` 时只按 `conversation_id`、未走仓储，若先删消息后删会话失败，消息会残留成孤儿；本项把顺序固定为"校验归属 → 删子表 → 删父表"，并放在同一事务（T1-09 保证回滚）。

## 验收标准

- `pytest backend/tests/repositories/test_owned_repository.py` 全部通过，含以下四类断言：
  - `await repo.get(db, "", obj_id)` 抛 `MissingOwnerError`；`await repo.get(db, None, obj_id)` 抛 `MissingOwnerError`；
  - 用户 A 建的记录，用 B 的 `user_id` 调 `get` 返回 `None`，调 `get_or_raise` 抛 `OwnershipError`；
  - `await repo.add(db, user_a, obj_with_user_id_b)` 抛 `OwnershipError`，且库中不新增行。
- 用用户 B 的 token 请求 A 的会话，返回 HTTP 403 与 `{"code":403,...}`，不返回 500，响应体不泄露"该资源存在"。
- `grep -c "OwnedMixin" backend/app/models/*.py` 覆盖 9 张应继承的表；`grep -c "OwnedMixin" backend/app/models/user.py` 为 0。
- `SELECT is_nullable FROM information_schema.columns WHERE table_name='ai_conversation' AND column_name='user_id'` 返回 `NO`。
- `grep -n "def .*session\|def raw\|def query" backend/app/repositories/base.py` 无输出（无逃生舱）。
- `mypy backend/app/repositories` 通过（泛型 `ModelT` 不产生 `Any` 泄露）。
