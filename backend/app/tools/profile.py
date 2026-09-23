"""农户档案检索工具（MCP）：拉取档案 + 作物 + 画像标签。

工具层接收 `Repositories | None` 而不是 `AsyncSession | None`：`app/agent/` 不得
直接依赖 ORM（L2 规则），传仓储对象使工具层签名里不出现 `AsyncSession`；
`None` 仍表示"无库上下文"，用于离线兜底。
"""

from collections.abc import Sequence

from app.models import Crop
from app.repositories import Repositories


async def query_profile(repos: Repositories | None, user_id: str) -> dict:
    """检索农户档案、作物与画像标签。"""
    profile = None
    crops: Sequence[Crop] = []
    tag = None
    if repos is not None:
        profile = await repos.profile.get_for_user(repos.db, user_id)
        crops = await repos.crop.list_for_profile(repos.db, user_id)
        tag = await repos.tag.get_for_user(repos.db, user_id)

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


__all__ = ["query_profile"]
