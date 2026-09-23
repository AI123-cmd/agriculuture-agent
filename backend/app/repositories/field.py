"""地块仓储。"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Field
from app.repositories.base import OwnedRepository


class FieldRepository(OwnedRepository[Field]):
    def __init__(self) -> None:
        super().__init__(Field)

    async def list_for_user(self, db: AsyncSession, user_id: str) -> Sequence[Field]:
        return await self.list_all(db, user_id, order_by=Field.field_name)


field_repo = FieldRepository()

__all__ = ["FieldRepository", "field_repo"]
