"""地点服务。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.integrations.geocoding import reverse_geocode
from app.integrations.weather import get_weather_summary
from app.models import Location
from app.schemas import SetLocationRequest


class LocationService:
    async def set_location(self, db: AsyncSession, user_id: str, req: SetLocationRequest) -> dict:
        loc = (await db.execute(select(Location).where(Location.user_id == user_id))).scalars().first()
        if loc is None:
            loc = Location(id=new_id("loc"), user_id=user_id)
            db.add(loc)

        loc.location_name = req.locationName
        loc.latitude = req.latitude
        loc.longitude = req.longitude
        loc.source = req.source
        await db.commit()

        return {
            "locationName": req.locationName,
            "weatherSummary": get_weather_summary(req.locationName),
        }

    def reverse_geocode(self, latitude: float, longitude: float) -> dict:
        return reverse_geocode(latitude, longitude)
