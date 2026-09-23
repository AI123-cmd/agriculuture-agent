"""地点服务。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.uow import unit_of_work
from app.integrations.geocoding import reverse_geocode
from app.integrations.weather import get_weather_summary
from app.repositories import location_repo
from app.schemas import SetLocationRequest


class LocationService:
    async def set_location(self, db: AsyncSession, user_id: str, req: SetLocationRequest) -> dict:
        loc = await location_repo.get_or_create(db, user_id)

        async with unit_of_work(db, name="location.set_location"):
            loc.location_name = req.locationName
            loc.latitude = req.latitude
            loc.longitude = req.longitude
            loc.source = req.source

        return {
            "locationName": req.locationName,
            "weatherSummary": get_weather_summary(req.locationName),
        }

    def reverse_geocode(self, latitude: float, longitude: float) -> dict:
        return reverse_geocode(latitude, longitude)


__all__ = ["LocationService"]
