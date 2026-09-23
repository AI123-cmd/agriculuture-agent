"""用户画像服务：标签更新、对话计数。

两个写方法都保持"不提交"约定——事务边界属于调用方的业务用例（T1-09 的
`unit_of_work`），服务方法只负责构造变更。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import tag_repo


class ProfileService:
    async def update_tags(self, db: AsyncSession, user_id: str, tags: list[str]) -> None:
        """覆盖式更新画像标签（不提交，由调用方 commit）。"""
        await tag_repo.replace_tags(db, user_id, tags)

    async def add_tags(self, db: AsyncSession, user_id: str, new_tags: list[str]) -> None:
        """增量合并标签并累加对话次数（不提交）。"""
        await tag_repo.merge_tags(db, user_id, new_tags)

    async def get_tags(self, db: AsyncSession, user_id: str) -> list[str]:
        tag = await tag_repo.get_for_user(db, user_id)
        return list(tag.tags) if tag and tag.tags else []


__all__ = ["ProfileService"]
