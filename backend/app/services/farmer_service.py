"""农户档案服务。"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.uow import unit_of_work
from app.models import Crop, FarmerProfile
from app.repositories import crop_repo, profile_repo
from app.schemas import ProfileRequest
from app.services.profile_service import ProfileService


def _float(v: float | None) -> float | None:
    return float(v) if v is not None else None


class FarmerService:
    async def get_profile(self, db: AsyncSession, user_id: str) -> dict:
        profile = await profile_repo.get_for_user(db, user_id)
        if profile is None:
            return {"hasProfile": False, "profile": None}

        crops = await crop_repo.list_for_profile(db, user_id)
        return {"hasProfile": True, "profile": self._build_profile(profile, crops)}

    async def save_profile(self, db: AsyncSession, user_id: str, req: ProfileRequest) -> dict:
        """保存档案与作物列表。

        全部写入收在同一个事务里：原实现在"清空作物"与"逐条新增"之间提交，若新增
        违反约束，删除已落库，农户的作物列表会被清空且无法恢复。这是本项最危险的
        一处结构性问题（T1-09 实现要点一）。
        """
        async with unit_of_work(db, name="farmer.save_profile"):
            profile = await profile_repo.get_or_create(db, user_id)

            profile.real_name = req.realName
            profile.age = req.age
            profile.gender = req.gender
            profile.phone = req.phone
            profile.experience_year = req.experienceYear
            profile.province = req.province
            profile.city = req.city
            profile.district = req.district
            profile.address = req.address
            profile.total_mu = req.totalMu
            profile.cultivation_type = req.cultivationType
            profile.water_source = req.waterSource
            profile.remark = req.remark

            # 覆盖作物列表，首条为主作物；清空与写入都在仓储内按归属执行
            await crop_repo.replace_all(
                db,
                user_id,
                [
                    {
                        "crop_name": item.cropName,
                        "plant_mu": item.plantMu,
                        "growth_stage": item.growthStage,
                    }
                    for item in req.crops
                ],
            )

            # 归档保存后刷新画像标签（不提交，同属本事务）
            await ProfileService().update_tags(db, user_id, [c.cropName for c in req.crops])

        return await self.get_profile(db, user_id)

    def _build_profile(self, profile: FarmerProfile, crops: Sequence[Crop]) -> dict:
        return {
            "realName": profile.real_name,
            "age": profile.age,
            "gender": profile.gender,
            "phone": profile.phone,
            "experienceYear": profile.experience_year,
            "province": profile.province,
            "city": profile.city,
            "district": profile.district,
            "address": profile.address,
            "totalMu": _float(profile.total_mu),
            "cultivationType": profile.cultivation_type,
            "waterSource": profile.water_source,
            "remark": profile.remark,
            "crops": [
                {
                    "cropName": c.crop_name,
                    "plantMu": _float(c.plant_mu),
                    "growthStage": c.growth_stage or "",
                }
                for c in crops
            ],
        }


__all__ = ["FarmerService"]
