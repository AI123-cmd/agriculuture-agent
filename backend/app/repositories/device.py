"""设备仓储。"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.repositories.base import OwnedRepository


class DeviceRepository(OwnedRepository[Device]):
    def __init__(self) -> None:
        super().__init__(Device)

    async def list_for_user(self, db: AsyncSession, user_id: str) -> Sequence[Device]:
        """本人设备列表（环境看板、播报快照都用它）。"""
        return await self.list_all(db, user_id)


device_repo = DeviceRepository()

__all__ = ["DeviceRepository", "device_repo"]
