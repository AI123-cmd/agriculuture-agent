"""农户档案检索工具（MCP）：拉取档案 + 作物 + 画像标签。"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Crop, FarmerProfile, UserProfileTag


async def query_profile(db: AsyncSession | None, user_id: str) -> dict:
    """检索农户档案、作物与画像标签。"""
    profile = None
    crops: Sequence[Crop] = []
    tag = None
    if db is not None:
        profile = (await db.execute(select(FarmerProfile).where(FarmerProfile.user_id == user_id))).scalars().first()
        crops = (await db.execute(select(Crop).where(Crop.user_id == user_id))).scalars().all()
        tag = (await db.execute(select(UserProfileTag).where(UserProfileTag.user_id == user_id))).scalars().first()

    def mask(name: str) -> str:
        return (name[0] + "**") if name else "农户"

    return {
        "farmer": {
            "name": mask(profile.real_name) if profile else "农户",
            "location": f"{profile.province or ''}{profile.city or ''}{profile.district or ''}" if profile else "",
            "experience_year": profile.experience_year if profile else 0,
        },
        "crops": [
            {"name": c.crop_name, "growth_stage": c.growth_stage, "plant_mu": float(c.plant_mu or 0)} for c in crops
        ],
        "tags": tag.tags if tag and tag.tags else [],
        "source": "farmer_profile",
    }
