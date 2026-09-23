"""地点相关模型。"""

from pydantic import BaseModel


class SetLocationRequest(BaseModel):
    locationName: str
    latitude: float | None = None
    longitude: float | None = None
    source: str = "manual"


class ReverseGeocodeResponse(BaseModel):
    province: str = ""
    city: str = ""
    district: str = ""
    fullAddress: str = ""
