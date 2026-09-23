"""用户服务：头像上传、注销账号。"""

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.storage import get_storage
from app.repositories import (
    broadcast_repo,
    conversation_repo,
    crop_repo,
    device_repo,
    field_repo,
    location_repo,
    memory_repo,
    message_repo,
    profile_repo,
    tag_repo,
    user_repo,
)


class UserService:
    async def upload_avatar(self, db: AsyncSession, user_id: str, file: UploadFile) -> str:
        data = file.file.read()
        url = get_storage().save(file.filename or "avatar", data)

        await user_repo.update_fields(db, user_id, avatar_url=url)
        await db.commit()
        return url

    async def cancel_account(self, db: AsyncSession, user_id: str) -> None:
        """注销账号：按依赖逆序删除，全程同一事务。

        逆序是硬要求：`ai_message` 经 `conversation_id` 指向 `ai_conversation`，
        必须先在子表侧删净，再删父表。消息的删除条件写成
        "`conversation_id` 属于本人会话"的子查询（`delete_by_user`），
        不再依赖"先取出 `conv_ids` 再 `in_(...)`"——那会在两步之间留下窗口，
        并发的会话行删除会让消息成为孤儿。

        不在这里 `commit()`：事务边界由 T1-09 统一收在服务层入口，
        避免出现"删了一半提交失败"的中间态。
        """
        await message_repo.delete_by_user(db, user_id)
        await conversation_repo.delete_all(db, user_id)
        await memory_repo.delete_all(db, user_id)
        await broadcast_repo.delete_all(db, user_id)
        await location_repo.delete_all(db, user_id)
        await device_repo.delete_all(db, user_id)
        await crop_repo.delete_all(db, user_id)
        await field_repo.delete_all(db, user_id)
        await profile_repo.delete_all(db, user_id)
        await tag_repo.delete_all(db, user_id)
        await user_repo.delete(db, user_id)


__all__ = ["UserService"]
