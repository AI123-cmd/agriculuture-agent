"""农事播报服务。"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import BroadcastLog


class BroadcastService:
    async def get_today(self, db: AsyncSession, user_id: str) -> dict:
        today = date.today().isoformat()
        items = (
            (
                await db.execute(
                    select(BroadcastLog)
                    .where(BroadcastLog.user_id == user_id, BroadcastLog.date == today)
                    .order_by(BroadcastLog.time)
                )
            )
            .scalars()
            .all()
        )
        if not items:
            items = await self._generate(db, user_id, today)
        return {"date": today, "items": [self._to_item(i) for i in items]}

    async def mark_done(self, db: AsyncSession, user_id: str, item_id: str) -> None:
        item = (
            (await db.execute(select(BroadcastLog).where(BroadcastLog.id == item_id, BroadcastLog.user_id == user_id)))
            .scalars()
            .first()
        )
        if item:
            item.done = 1
            await db.commit()

    async def _generate(self, db: AsyncSession, user_id: str, today: str) -> list[BroadcastLog]:
        from app.broadcast import generate_broadcast  # 延迟导入，避免包间循环依赖

        results = await generate_broadcast(db, user_id)

        items = []
        for idx, r in enumerate(results):
            item = BroadcastLog(
                id=new_id("bc"),
                user_id=user_id,
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
            db.add(item)
            items.append(item)
        await db.commit()
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
