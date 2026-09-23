"""地点仓储：一人一条当前地点。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import Location
from app.repositories.base import OwnedRepository


class LocationRepository(OwnedRepository[Location]):
    def __init__(self) -> None:
        super().__init__(Location)

    async def get_for_user(self, db: AsyncSession, user_id: str) -> Location | None:
        return await self.first(db, user_id)

    async def get_or_create(self, db: AsyncSession, user_id: str) -> Location:
        """取不到就建一行：地点是"有则更新、无则新建"的单行资源。"""
        loc = await self.get_for_user(db, user_id)
        if loc is None:
            loc = Location(id=new_id("loc"))
            await self.add(db, user_id, loc)
        return loc


location_repo = LocationRepository()

__all__ = ["LocationRepository", "location_repo"]
