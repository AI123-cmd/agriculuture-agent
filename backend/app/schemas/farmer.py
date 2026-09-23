"""农户档案相关模型。"""
from pydantic import BaseModel


class CropItem(BaseModel):
    cropName: str
    plantMu: float | None = None
    growthStage: str = ""


class FarmProfile(BaseModel):
    realName: str
    age: int | None = None
    gender: int = 0
    phone: str = ""
    experienceYear: int = 0
    province: str = ""
    city: str = ""
    district: str = ""
    address: str = ""
    totalMu: float = 0
    cultivationType: int = 0
    waterSource: str = ""
    remark: str = ""
    crops: list[CropItem] = []


class ProfileRequest(FarmProfile):
    """POST /api/farmer/profile 请求体。"""


class FarmProfileResponse(BaseModel):
    hasProfile: bool
    profile: FarmProfile | None = None