"""用户服务：头像上传、注销账号。"""

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.storage import get_storage
from app.models import (
    AIConversation,
    AIMessage,
    BroadcastLog,
    Crop,
    Device,
    FarmerProfile,
    Field,
    Location,
    MemorySummary,
    User,
    UserProfileTag,
)


class UserService:
    async def upload_avatar(self, db: AsyncSession, user_id: str, file: UploadFile) -> str:
        data = file.file.read()
        url = get_storage().save(file.filename or "avatar", data)

        user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        if user:
            user.avatar_url = url
        await db.commit()
        return url

    async def cancel_account(self, db: AsyncSession, user_id: str) -> None:
        # 先删会话消息（依赖会话 id）
        conv_ids = (
            (await db.execute(select(AIConversation.id).where(AIConversation.user_id == user_id))).scalars().all()
        )
        if conv_ids:
            await db.execute(delete(AIMessage).where(AIMessage.conversation_id.in_(conv_ids)))
        await db.execute(delete(AIConversation).where(AIConversation.user_id == user_id))
        await db.execute(delete(MemorySummary).where(MemorySummary.user_id == user_id))
        await db.execute(delete(BroadcastLog).where(BroadcastLog.user_id == user_id))
        await db.execute(delete(Location).where(Location.user_id == user_id))
        await db.execute(delete(Device).where(Device.user_id == user_id))
        await db.execute(delete(Crop).where(Crop.user_id == user_id))
        await db.execute(delete(Field).where(Field.user_id == user_id))
        await db.execute(delete(FarmerProfile).where(FarmerProfile.user_id == user_id))
        await db.execute(delete(UserProfileTag).where(UserProfileTag.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
