"""农户档案服务。"""
from sqlalchemy.orm import Session

from app.db.base import new_id
from app.models import Crop, FarmerProfile
from app.schemas import ProfileRequest
from app.services.profile_service import ProfileService


def _float(v):
    return float(v) if v is not None else None


class FarmerService:
    def get_profile(self, db: Session, user_id: str) -> dict:
        profile = db.query(FarmerProfile).filter(FarmerProfile.user_id == user_id).first()
        if profile is None:
            return {"hasProfile": False, "profile": None}

        crops = db.query(Crop).filter(Crop.user_id == user_id).order_by(Crop.is_main.desc(), Crop.create_time).all()
        return {"hasProfile": True, "profile": self._build_profile(profile, crops)}

    def save_profile(self, db: Session, user_id: str, req: ProfileRequest) -> dict:
        profile = db.query(FarmerProfile).filter(FarmerProfile.user_id == user_id).first()
        if profile is None:
            profile = FarmerProfile(id=new_id("fp"), user_id=user_id)
            db.add(profile)

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

        # 覆盖作物列表，首条为主作物
        db.query(Crop).filter(Crop.user_id == user_id).delete()
        for i, item in enumerate(req.crops):
            db.add(
                Crop(
                    id=new_id("crop"),
                    user_id=user_id,
                    crop_name=item.cropName,
                    plant_mu=item.plantMu or 0,
                    growth_stage=item.growthStage or "",
                    is_main=1 if i == 0 else 0,
                )
            )

        # 归档保存后刷新画像标签
        ProfileService().update_tags(db, user_id, [c.cropName for c in req.crops])

        db.commit()
        return self.get_profile(db, user_id)

    def _build_profile(self, profile: FarmerProfile, crops: list[Crop]) -> dict:
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