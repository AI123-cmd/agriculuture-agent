"""用户服务：头像上传、注销账号。"""

import logging

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import BadRequest
from app.core.validators import validate_image
from app.db.uow import unit_of_work
from app.integrations.storage import get_storage
from app.repositories import (
    conversation_repo,
    crop_repo,
    device_repo,
    field_repo,
    ledger_repo,
    location_repo,
    memory_repo,
    message_repo,
    profile_repo,
    tag_repo,
    user_repo,
)

logger = logging.getLogger(__name__)


class UserService:
    async def upload_avatar(self, db: AsyncSession, user_id: str, file: UploadFile) -> str:
        """保存头像并更新用户记录。

        校验必须插在落盘之前：原实现先 `save` 再落库，任何文件都会被写进公开可访问
        的 `/static/` 目录，农户能上传可执行脚本并拿到一个合法 URL。现在"大小 → 真实
        魔数 → 与声明类型一致"三重校验不过就抛 400，一个字节都不落盘。

        文件先落盘、再进事务：对象存储不在数据库事务的管辖范围内（远端 OSS 更是无法
        参与本地事务），所以顺序只能是"先文件后记录"。若记录更新失败，留下的是一个
        无人引用的孤儿文件——比"数据库指向不存在的文件"安全得多。
        """
        settings = get_settings()
        # `UploadFile.size` 由 Starlette 从 multipart 头解析，可能缺失或被伪造，
        # 因此只用它做"提前拒绝"的粗筛，真正的判定在 `validate_image` 里对 `data` 做
        if file.size and file.size > settings.UPLOAD_MAX_BYTES:
            raise BadRequest(f"文件超过上限 {settings.UPLOAD_MAX_BYTES // 1024 // 1024}MB")

        data = await file.read()
        check = validate_image(
            file.filename or "",
            file.content_type,
            data,
            max_bytes=settings.UPLOAD_MAX_BYTES,
            allowed=settings.UPLOAD_ALLOWED_TYPES,
        )
        if not check.ok or check.mime is None:
            logger.warning("头像上传被拒：user=%s file=%s reason=%s", user_id, file.filename, check.reason)
            raise BadRequest(check.reason or "图片校验失败")

        url = get_storage().save_image(data, check.mime)

        async with unit_of_work(db, name="user.upload_avatar"):
            await user_repo.update_fields(db, user_id, avatar_url=url)
        return url

    async def cancel_account(self, db: AsyncSession, user_id: str) -> None:
        """注销账号：按依赖逆序删除，全程同一事务。

        逆序是硬要求：`ai_message` 经 `conversation_id` 指向 `ai_conversation`，
        必须先在子表侧删净，再删父表。消息的删除条件写成
        "`conversation_id` 属于本人会话"的子查询（`delete_by_user`），
        不再依赖"先取出 `conv_ids` 再 `in_(...)`"——那会在两步之间留下窗口，
        并发的会话行删除会让消息成为孤儿。

        `unit_of_work` 是必需的：11 张表的删除必须"全成或全不成"。若中途失败而某些
        表已提交，"用户已注销但数据残留"这种状态既违反合规要求，也让用户无法重新注册。
        """
        async with unit_of_work(db, name="user.cancel_account"):
            await message_repo.delete_by_user(db, user_id)
            await conversation_repo.delete_all(db, user_id)
            await memory_repo.delete_all(db, user_id)
            await ledger_repo.delete_all(db, user_id)
            await location_repo.delete_all(db, user_id)
            await device_repo.delete_all(db, user_id)
            await crop_repo.delete_all(db, user_id)
            await field_repo.delete_all(db, user_id)
            await profile_repo.delete_all(db, user_id)
            await tag_repo.delete_all(db, user_id)
            await user_repo.delete(db, user_id)


__all__ = ["UserService"]
