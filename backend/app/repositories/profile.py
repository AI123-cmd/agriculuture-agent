"""农户档案仓储：一人一档，`user_id` 上有唯一约束。"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import FarmerProfile
from app.repositories.base import OwnedRepository


class FarmerProfileRepository(OwnedRepository[FarmerProfile]):
    def __init__(self) -> None:
        super().__init__(FarmerProfile)

    async def get_for_user(self, db: AsyncSession, user_id: str) -> FarmerProfile | None:
        """取本人档案（一人一档，等价于 `first`，取名是为了读起来贴合业务）。"""
        return await self.first(db, user_id)

    async def get_or_create(self, db: AsyncSession, user_id: str) -> FarmerProfile:
        """取不到就建一行待插入对象。

        不在这里 `flush`：调用方紧接着会逐字段赋值，`flush` 只会多一次往返。
        `user_id` 由 `add` 强制写入，调用方无法"忘记归属"。
        """
        profile = await self.get_for_user(db, user_id)
        if profile is None:
            profile = FarmerProfile(id=new_id("fp"))
            await self.add(db, user_id, profile)
        return profile

    async def list_all_for_user(self, db: AsyncSession, user_id: str) -> Sequence[FarmerProfile]:
        return await self.list_all(db, user_id)


profile_repo = FarmerProfileRepository()

__all__ = ["FarmerProfileRepository", "profile_repo"]
