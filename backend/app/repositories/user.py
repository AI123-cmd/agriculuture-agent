"""用户仓储：`user` 表是归属主体自身，不继承 `OwnedMixin`。

它进不了 `OwnedRepository`（没有 `user_id` 列，`id` 本身就是归属），因此单独实现，
并把 `id` 当作归属参数使用——这与 L4 规则的白名单一致：`app/repositories/` 是
唯一允许直接构造语句的目录。
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories.base import execute_write, require_user_id


class UserRepository:
    """用户仓储。所有方法的 `user_id` 即 `user.id`，因此不需要额外的归属列。"""

    model = User

    async def get(self, db: AsyncSession, user_id: str) -> User | None:
        uid = require_user_id("User", user_id)
        return (await db.execute(select(User).where(User.id == uid))).scalars().first()

    async def get_by_openid(self, db: AsyncSession, openid: str) -> User | None:
        """登录用：按 openid 查用户。这里尚无"当前用户"，故不接受 user_id。"""
        return (await db.execute(select(User).where(User.openid == openid))).scalars().first()

    async def add(self, db: AsyncSession, user: User) -> User:
        """新建用户。`id` 由调用方生成（登录流程要在签发 token 前拿到 id）。"""
        db.add(user)
        return user

    async def update_fields(self, db: AsyncSession, user_id: str, **fields: object) -> int:
        """更新用户字段，按 `id` 过滤（`id` 即归属，等价于归属条件）。"""
        uid = require_user_id("User", user_id)
        user = await self.get(db, uid)
        if user is None:
            return 0
        for field, value in fields.items():
            setattr(user, field, value)
        return 1

    async def delete(self, db: AsyncSession, user_id: str) -> int:
        uid = require_user_id("User", user_id)
        return await execute_write(db, delete(User).where(User.id == uid))


user_repo = UserRepository()

__all__ = ["UserRepository", "user_repo"]
