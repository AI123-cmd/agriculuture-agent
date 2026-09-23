"""农事播报服务。"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.db.uow import unit_of_work
from app.models import BroadcastLog
from app.repositories import Repositories, broadcast_repo


class BroadcastService:
    async def get_today(self, db: AsyncSession, user_id: str) -> dict:
        today = date.today().isoformat()
        items = list(await broadcast_repo.list_by_date(db, user_id, today))
        if not items:
            items = await self._generate(db, user_id, today)
        return {"date": today, "items": [self._to_item(i) for i in items]}

    async def mark_done(self, db: AsyncSession, user_id: str, item_id: str) -> None:
        """标记完成。归属不符时影响 0 行、不抛异常：重复点击不该报错。"""
        async with unit_of_work(db, name="broadcast.mark_done"):
            await broadcast_repo.mark_done(db, user_id, item_id)

    async def _generate(self, db: AsyncSession, user_id: str, today: str) -> list[BroadcastLog]:
        """生成当日播报。批量写入收在一个事务里：部分成功会让农户看到半份播报。"""
        from app.broadcast import generate_broadcast  # 延迟导入，避免包间循环依赖

        results = await generate_broadcast(Repositories(db), user_id)

        items: list[BroadcastLog] = []
        async with unit_of_work(db, name="broadcast.generate"):
            for idx, r in enumerate(results):
                item = BroadcastLog(
                    id=new_id("bc"),
                    date=today,
                    time=f"{9 + idx * 2:02d}:00",
                    title=r.title[:64],
                    content=r.content[:500],
                    type=r.type[:32],
                    level=r.level or "normal",
                    source=r.source or "sensor",
                    meta=r.meta,
                    done=0,
                )
                await broadcast_repo.add(db, user_id, item)
                items.append(item)
        return items

    def _to_item(self, item: BroadcastLog) -> dict:
        return {
            "id": item.id,
            "time": item.time or "",
            "title": item.title,
            "content": item.content,
            "done": bool(item.done),
            "type": item.type,
            "level": item.level,
            "source": item.source,
        }


__all__ = ["BroadcastService"]
