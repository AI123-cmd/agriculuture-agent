"""画像标签仓储：一人一份画像，`user_id` 上有唯一约束。"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.models import UserProfileTag
from app.repositories.base import OwnedRepository


class ProfileTagRepository(OwnedRepository[UserProfileTag]):
    def __init__(self) -> None:
        super().__init__(UserProfileTag)

    async def get_for_user(self, db: AsyncSession, user_id: str) -> UserProfileTag | None:
        return await self.first(db, user_id)

    async def get_or_create(self, db: AsyncSession, user_id: str) -> UserProfileTag:
        """取不到就建一行（初始标签为空、对话次数为 0）。

        两个服务方法（覆盖式更新与增量合并）都从这里拿对象，避免各写一遍
        "查不到就 new"的分支——那正是现状 `profile_service` 里重复两次的代码。
        """
        tag = await self.get_for_user(db, user_id)
        if tag is None:
            tag = UserProfileTag(id=new_id("tag"), tags=[], chat_count=0)
            await self.add(db, user_id, tag)
        return tag

    async def replace_tags(self, db: AsyncSession, user_id: str, tags: list[str]) -> UserProfileTag:
        """覆盖式更新标签（不提交，由调用方 commit）。"""
        tag = await self.get_or_create(db, user_id)
        tag.tags = sorted({t for t in tags if t})
        return tag

    async def merge_tags(self, db: AsyncSession, user_id: str, new_tags: list[str]) -> UserProfileTag:
        """增量合并标签并累加对话次数（不提交）。"""
        tag = await self.get_or_create(db, user_id)
        tag.tags = sorted({*(tag.tags or []), *(t for t in new_tags if t)})
        tag.chat_count = (tag.chat_count or 0) + 1
        tag.last_chat_at = datetime.now()
        return tag


tag_repo = ProfileTagRepository()

__all__ = ["ProfileTagRepository", "tag_repo"]
