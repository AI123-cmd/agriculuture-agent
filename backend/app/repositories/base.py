"""归属安全仓储基类：所有查询强制注入归属条件。

铁律：任何查询方法都必须显式接收 `user_id`；缺失即抛异常，不做降级。
`_require_user` 与 `_owned_query` 是构造查询的唯一出口，因此 `user_id=""`、
`None`、非字符串三种"看起来像有参数"的情况都会被拒。基类不提供任何返回
裸 `AsyncSession` 或原始查询语句的逃生舱。
"""

from collections.abc import Sequence
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import ColumnElement, CursorResult, Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import MissingOwnerError, OwnershipError
from app.db.owned import OwnedModel

ModelT = TypeVar("ModelT", bound=OwnedModel)


def require_user_id(model_name: str, user_id: str | None) -> str:
    """空串、None、非字符串一律拒绝，不做降级。

    做成模块级函数而非仅基类方法，是为了让"模型不带 user_id"的仓储
    （如 `app/repositories/conversation.py` 里管 `ai_message` 的子表仓储）
    也能复用同一套判定，避免各写一份而出现口径漂移。
    """
    if not user_id or not isinstance(user_id, str):
        raise MissingOwnerError(f"{model_name} 缺少归属参数 user_id，已拒绝执行")
    return user_id


async def execute_write(db: AsyncSession, stmt: Any) -> int:
    """执行 DML 并取受影响行数。

    `AsyncSession.execute` 的类型标注返回 `Result[Any]`，而实际对象是
    `CursorResult`（`rowcount` 只在后者上）。这里的 cast 是补标注，不是绕过检查。
    """
    result = cast(CursorResult[Any], await db.execute(stmt))
    return result.rowcount


class OwnedRepository(Generic[ModelT]):
    """归属安全仓储基类。"""

    model: type[ModelT]

    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    def _require_user(self, user_id: str | None) -> str:
        return require_user_id(self.model.__name__, user_id)

    def _owned_query(self, user_id: str) -> Select[tuple[ModelT]]:
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

    async def list(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        order_by: ColumnElement[Any] | None = None,
    ) -> Sequence[ModelT]:
        stmt = self._owned_query(user_id)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        return (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    async def list_all(self, db: AsyncSession, user_id: str, **conditions: Any) -> Sequence[ModelT]:
        """不分页取全部（注销账号等内部流程用），仍强制带归属条件。"""
        stmt = self._owned_query(user_id)
        for field, value in conditions.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        return (await db.execute(stmt)).scalars().all()

    async def count(self, db: AsyncSession, user_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.user_id == self._require_user(user_id))
        )
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
        stmt = update(self.model).where(self.model.id == obj_id, self.model.user_id == uid).values(**fields)
        return await execute_write(db, stmt)

    async def delete_all(self, db: AsyncSession, user_id: str, **conditions: Any) -> int:
        stmt = delete(self.model).where(self.model.user_id == self._require_user(user_id))
        for field, value in conditions.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        return await execute_write(db, stmt)

    async def delete(self, db: AsyncSession, user_id: str, obj_id: str) -> int:
        return await self.delete_all(db, user_id, id=obj_id)
