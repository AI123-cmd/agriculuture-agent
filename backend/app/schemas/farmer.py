"""农户档案相关模型。"""

from datetime import date

from pydantic import BaseModel


class CropItem(BaseModel):
    """一条经营条目。

    `sowingDate` 是农事历推算的起始时间（`Crop.sowing_date`）：缺它则
    `load_calendar_entries` → `resolve_stage` 返回 `None`，首页农事历整条不产出。
    `processType` 是农户在档案页选的流程类型（七选一，取 `ProcessId.value`）；
    非空且非 `other` 时服务层用它把 `crop_name` 归一为该流程的展示名，使
    `object_aliases` 稳定命中；为空时按 `cropName` 自由匹配（兼容旧数据）。
    """

    cropName: str
    processType: str = ""
    cropVariety: str | None = None
    plantMu: float | None = None
    sowingDate: date | None = None
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
