# T1-05 OwnedMixin 与 OwnedRepository 归属安全基类

## 这一项要做什么

建立"归属安全"的工程基座：所有业务表统一通过 `OwnedMixin` 携带 `user_id`，所有数据访问统一通过 `OwnedRepository` 并强制注入归属条件，缺少 `user_id` 参数时直接抛异常。这是产品说明书 9.2 第 1 条验收项（AI 诊断的数据写入未校验归属，存在越权风险）在架构层的解法——仅靠代码评审无法长期维持，必须让"忘记加过滤"在运行时不可能通过。

现有 42 处 `db.query(Model)` 散落在 `app/services/`、`app/tools/`、`app/broadcast/`，其中 `ai_service._persist` 按 `sessionId` 查会话、未校验该会话是否属于当前用户，是越权的实际入口。本项只建基类与异常，不改调用点（T1-06 做全量整改）。

## 前置依赖

- 依赖 T1-02 已提供：异步 `AsyncSession`、`async_sessionmaker`、带时区的 `TimestampMixin`。
- 依赖 T1-04 已提供：真实迁移链路，使新增的 `user_id` 索引与约束可追溯。
- 外部环境：PostgreSQL 16+（`OwnedRepository` 的部分实现用到 `SELECT ... FOR UPDATE`）。
- 需要人工确认：技术方案 12.7 把"无外键设计"列为待补，本项在 T1-04 补的外键基础上，为 `OwnedMixin` 的每张表确认是否需要有外键到 `user`。用户表本身与 `process_config` / `knowledge_item` 等无归属表**不继承** `OwnedMixin`。

## 参考章节

- 技术方案 第12章 12.3 用户标识与归属安全（`OwnedMixin` / `OwnedRepository` 原文与"无 user_id 则抛异常"）
- 技术方案 第15章 15.2 分层与依赖约束（第 4 条硬规则：任何模块不得直接 `db.query(Model)`）
- 技术方案 第16章 16.2 第一步：底座归位（技术动作 4：归属校验统一）
- 技术方案 第12章 12.1 表清单总览、12.4 问诊与处方卡、12.5 提示与触达、12.6 环境数据三表（哪些表带 `user_id`）
- 产品说明书 9.2 第 1 条；9.4（外键约束、表清单）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/app/db/owned.py` | `OwnedMixin` 定义与 `OwnedModel` 协议 |
| 新增 | `backend/app/repositories/__init__.py` | 仓储包入口，导出各仓储单例 |
| 新增 | `backend/app/repositories/base.py` | `OwnedRepository` 泛型基类，强制归属条件 |
| 新增 | `backend/app/repositories/conversation.py` | `ConversationRepository`（历史列表性能优化底座） |
| 新增 | `backend/app/core/ownership.py` | `OwnershipError` / `MissingOwnerError` 异常 |
| 修改 | `backend/app/core/exceptions.py` | 增加 `OwnershipError` 映射 403 的处理器 |
| 修改 | `backend/app/models/__init__.py` | 各模型改继承 `OwnedMixin`（模型侧，见实现要点四） |
| 新增 | `backend/tests/repositories/test_owned_repository.py` | 断言缺少 `user_id` 抛异常、跨用户查不到 |
| 新增 | `backend/tests/test_ownership_403.py` | 断言异常映射为 403 而非 500 |

## 实现要点

### 一、OwnedMixin 的完整代码骨架

`app/db/owned.py`：

```python
"""归属安全的模型混入：所有业务表统一带 user_id 字段。

继承本混入的模型，其全部数据访问必须经 OwnedRepository，
由 T1-12 的分层检查脚本强制（禁止 db.query(Model)）。
"""
from sqlalchemy import String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class OwnedMixin:
    """归属字段混入。

    declared_attr 用于让子类可覆写字段定义（如查询表需要额外索引），
    同时保证 user_id 在每张表上都是 NOT NULL + 索引。
    """

    @declared_attr
    def user_id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(
            String(36),
            nullable=False,
            index=True,
            comment="归属用户ID",
        )
```

三个设计取舍：

第一，`user_id` 长度取 `String(36)`，全局约定要求主键统一 `String(36)`。现状 `app/models/user.py` 用 `String(32)`、`crop.py` 与 `conversation.py` 也用 32，T1-04 已在基线迁移统一为 36，本混入不能在长度上开倒车。

第二，用 `declared_attr` 而不是普通 `mapped_column`。普通写法在多个子类继承同一混入时，SQLAlchemy 会复用同一个 `Column` 对象并报"Column object already assigned to Table"。`declared_attr` 为每个子类生成独立列对象，这是 SQLAlchemy 2.0 混入的标准写法。

第三，`index=True` 写进混入。全局约定明确"所有业务表带 `user_id`，索引必建"。把它放在混入里，新增表自动带上，不依赖执行者记得加索引——这正是产品说明书 9.1"归属条件必须带"要的那种"收敛到公共层"的做法。

为让类型检查配合，同时给出协议：

```python
from typing import Protocol


class OwnedModel(Protocol):
    """供 OwnedRepository 做类型约束的最小协议。"""

    id: Mapped[str]
    user_id: Mapped[str]
```

### 二、OwnedRepository 的完整代码骨架

`app/repositories/base.py`：

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
    """归属安全仓储。

    铁律：任何查询方法都必须显式接收 user_id；缺失即抛异常，不做"跳过过滤"的降级。
    这不是防御式编程，而是让越权在运行时不可达——产品说明书 9.2 第 1 条。
    """

    model: type[ModelT]

    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    # ---- 基础过滤 ----

    def _require_user(self, user_id: str | None) -> str:
        """校验归属参数。空串、None、非字符串一律拒绝。"""
        if not user_id or not isinstance(user_id, str):
            raise MissingOwnerError(
                f"{self.model.__name__} 的查询缺少归属参数 user_id，已拒绝执行"
            )
        return user_id

    def _owned_query(self, user_id: str) -> Select:
        """构造带归属条件的 SELECT，禁止调用方自行拼 where。"""
        uid = self._require_user(user_id)
        return select(self.model).where(self.model.user_id == uid)

    # ---- 读 ----

    async def get(self, db: AsyncSession, user_id: str, obj_id: str) -> ModelT | None:
        """按主键取单条，归属不符返回 None（不区分"不存在"与"不属于你"）。"""
        stmt = self._owned_query(user_id).where(self.model.id == obj_id)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def get_or_raise(self, db: AsyncSession, user_id: str, obj_id: str) -> ModelT:
        """取单条，不存在或无权统一抛 OwnershipError（避免通过错误码探测他人数据）。"""
        obj = await self.get(db, user_id, obj_id)
        if obj is None:
            raise OwnershipError("资源不存在或无权访问")
        return obj

    async def list(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        order_by=None,
    ) -> Sequence[ModelT]:
        stmt = self._owned_query(user_id)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.limit(limit).offset(offset)
        return (await db.execute(stmt)).scalars().all()

    async def count(self, db: AsyncSession, user_id: str) -> int:
        uid = self._require_user(user_id)
        stmt = select(func.count()).select_from(self.model).where(self.model.user_id == uid)
        return (await db.execute(stmt)).scalar_one()

    # ---- 写 ----

    async def add(self, db: AsyncSession, user_id: str, obj: ModelT) -> ModelT:
        """新增：强制写入归属，禁止调用方传入与他人的 user_id。"""
        uid = self._require_user(user_id)
        if getattr(obj, "user_id", None) not in (None, uid):
            raise OwnershipError("新增对象的 user_id 与当前用户不一致")
        obj.user_id = uid
        db.add(obj)
        return obj

    async def update_fields(
        self, db: AsyncSession, user_id: str, obj_id: str, **fields: Any
    ) -> int:
        """按归属条件更新，返回受影响行数。"""
        uid = self._require_user(user_id)
        stmt = (
            update(self.model)
            .where(self.model.id == obj_id, self.model.user_id == uid)
            .values(**fields)
        )
        return (await db.execute(stmt)).rowcount

    async def delete(self, db: AsyncSession, user_id: str, obj_id: str) -> int:
        uid = self._require_user(user_id)
        stmt = delete(self.model).where(self.model.id == obj_id, self.model.user_id == uid)
        return (await db.execute(stmt)).rowcount
```

四个关键点，逐条说明为什么这样写：

**"缺少 user_id 参数即抛异常"的实现位置。** 不在于调用方是否记得传，而在于 `_require_user` 与 `_owned_query` 是唯一构造查询的出口。任何查询方法第一步都过 `_require_user`，因此 `user_id=""`、`user_id=None`、`user_id=0` 三种"看起来像有参数"的情况都会被拒。技术方案 12.3 给的原型是 `if not user_id: raise OwnershipError(...)`，本项把它抽成 `MissingOwnerError` 子类，是为了让"忘记传参"（编程错误）与"归属不符"（业务结果）在日志里可区分——前者应触发告警，后者是正常的 403。

**`get` 返回 None 而不抛异常，`get_or_raise` 才抛。** 列表场景需要"查不到就跳过"，单条详情场景需要"查不到就 403"。同时 `get_or_raise` 对"不存在"与"不属于你"返回同一个错误，避免攻击者用 404/403 的差异去枚举他人 ID。这是对技术方案的一处推断，方案未讨论错误码语义。

**禁止暴露 `db.query`。** 基类不提供任何返回裸 `Session` 的方法，不实现 `_raw_query` 之类的逃生舱。评审者看到"没有逃生舱"才能确信 L4 规则（任何模块不得直接使用 `db.query(Model)`）是有效的。

**`add` 覆写归属。** 现有 `ai_service._persist` 允许 `AIConversation(id=req.sessionId, user_id=user_id, ...)` 由调用方传 user_id；如果调用方传错，数据就落到别人名下。基类把 `obj.user_id` 强制设为当前 `user_id`，并拒绝"传了别人的 user_id"的情况。

### 三、异常与错误码

`app/core/ownership.py`：

```python
from app.core.exceptions import AppException


class OwnershipError(AppException):
    """归属校验失败：资源不存在或不属于当前用户。"""

    def __init__(self, message: str = "资源不存在或无权访问"):
        super().__init__(403, message)


class MissingOwnerError(OwnershipError):
    """编程错误：仓储调用未传 user_id。"""

    def __init__(self, message: str = "缺少归属参数，拒绝执行查询"):
        super().__init__(message)
```

现有 `app/core/exceptions.py` 只有 `AppException` / `Unauthorized` / `NotFound` / `ServerError`，且 `main.py` 的 `app_exception_handler` 用 `exc.code` 同时当业务码与 HTTP 状态码（`error_response(exc.code, exc.message, status_code=exc.code)`）。`OwnershipError` 取 403 即可复用这套映射，不需要改 `main.py`。注意现状有个隐患：`AppException` 用 500 作为默认值，`ServerError` 也是 500，本项不要顺手改它，否则会牵动 T1-09 的事务边界任务。

### 四、模型侧的最小改动

`OwnedMixin` 只有在模型继承它之后才有意义。本项的模型改动要"只加不改"：

```python
class AIConversation(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "ai_conversation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # user_id 由 OwnedMixin 提供，删除手写的 user_id 列
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    crop_tag: Mapped[str | None] = mapped_column(String(32))
```

需要继承 `OwnedMixin` 的表（技术方案 12.1 中带 `user_id` 的全部）：`farmer_profile`、`crop`、`field`、`device`、`location`、`broadcast_log`、`ai_conversation`、`memory_summary`、`user_profile_tag`。不继承的：`user`（归属主体自身）、`ai_message`（通过 `conversation_id` 间接归属，父表已校验；直接继承会引入冗余且 T1-11 的批量查询要绕开归属列）、未来新增的 `process_config` / `knowledge_item` / `knowledge_region` / `vendor_template`（全局数据，无归属）。

`ai_message` 的处理需要额外说明：它没有 `user_id` 列，因此无法继承 `OwnedMixin`，也就无法进 `OwnedRepository`。技术方案 12.3 只要求"所有业务表带 `user_id`"，`ai_message` 是"子表"。本项给它单独一个 `ConversationScopedRepository`，其 `_require_user` 先校验父会话归属，再查消息。这是对技术方案的一处推断，方案未涉及子表形态；T1-06 会用到这个类。

### 五、ConversationRepository

`app/repositories/conversation.py`，作为 T1-11 的底座：

```python
class ConversationRepository(OwnedRepository[AIConversation]):
    def __init__(self) -> None:
        super().__init__(AIConversation)

    async def delete_with_messages(self, db: AsyncSession, user_id: str, conv_id: str) -> int:
        """删除会话及其消息：先校验归属，再删子表，最后删父表。"""
        conv = await self.get(db, user_id, conv_id)
        if conv is None:
            raise OwnershipError("会话不存在或无权访问")
        await db.execute(delete(AIMessage).where(AIMessage.conversation_id == conv_id))
        return await self.delete(db, user_id, conv_id)

    async def paginate_with_message_count(
        self, db: AsyncSession, user_id: str, page: int, size: int
    ) -> tuple[list[tuple[AIConversation, int, str | None]], int]:
        """一次查询取会话 + 消息数 + 最后一条预览（T1-11 的 N+1 消除）。"""
        ...
```

`delete_with_messages` 的现状缺陷在 `ai_service.delete_history`：它查 `AIConversation.id == session_id` 时**没有**校验 `user_id` 归属（第 108 行虽有 `.filter(AIConversation.user_id == user_id)`，但 `AIMessage` 的 `delete` 只按 `conversation_id`，如果先删消息后删会话失败，消息会残留），且删除消息未走仓储。本类把它收敛为"先校验归属 → 删子表 → 删父表"的固定顺序，并把删除放在同一事务里（T1-09 保证回滚）。

### 六、与验收项的对应

- 直接对应产品说明书 9.2 第 1 条（AI 诊断的数据写入未校验归属）：`add` 强制写入当前用户归属，`_require_user` 杜绝无过滤查询。
- 对应技术方案 15.2 第 4 条硬规则（任何模块不得直接 `db.query(Model)`）：本项提供唯一合法入口，T1-12 提供检查脚本。
- 为产品说明书 9.1 数据层"所有数据访问必须带归属条件"提供可执行落地。
- 为技术方案 6.7 转人工、12.4 处方卡的"历史可追溯且不越权"提供边界：处方卡与回访任务同样继承 `OwnedMixin`。

## 验收标准

- `pytest backend/tests/repositories/test_owned_repository.py` 全部通过，且至少包含以下四类断言：
  - `await repo.get(db, "", obj_id)` 抛 `MissingOwnerError`；
  - `await repo.get(db, None, obj_id)` 抛 `MissingOwnerError`；
  - 用户 A 建的记录，用用户 B 的 `user_id` 调 `get` 返回 `None`，调 `get_or_raise` 抛 `OwnershipError`；
  - `await repo.add(db, user_a, obj_with_user_id_b)` 抛 `OwnershipError`，且库中不新增行。
- `curl -X GET /api/ai/history/{他人会话id}` 返回 HTTP 403 与 `{"code":403,...}`，不返回 500，且响应体不泄露"该资源存在"。
- `grep -c "OwnedMixin" backend/app/models/*.py` 覆盖 9 张应继承的表，`grep -c "OwnedMixin" backend/app/models/user.py` 为 0。
- 数据库层面：`SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='ai_conversation' AND column_name='user_id'` 返回 `user_id | NO`，且 `SELECT indexname FROM pg_indexes WHERE tablename='ai_conversation'` 含 `user_id` 索引。
- `OwnedRepository` 中不存在任何返回 `AsyncSession` 或裸 `Select` 之外可绕过归属的公开方法：`grep -n "def .*session\|def raw\|def query" backend/app/repositories/base.py` 无输出。
- `mypy backend/app/repositories` 通过（泛型 `ModelT` 不产生 `Any` 泄露）。
