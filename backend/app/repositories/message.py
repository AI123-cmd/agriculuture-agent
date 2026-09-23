"""`ai_message` 的子表仓储：归属经父会话间接判定。

`ai_message` 没有 `user_id` 列（它经 `conversation_id` 归属），因此进不了
`OwnedRepository`。所有读写都以"父会话属于当前用户"为唯一过滤入口，用子查询写
条件而不是先查父表再判断——先查后判会在两步之间留下竞态窗口，子查询则与语句同时
生效。
"""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import OwnershipError
from app.models import AIConversation, AIMessage
from app.repositories.base import execute_write, require_user_id


class ConversationScopedRepository:
    """消息仓储。缺 `user_id` 与归属不符分别抛 `MissingOwnerError` / `OwnershipError`。"""

    model = AIMessage

    def _owned_conversation_ids(self, user_id: str | None) -> Select[tuple[str]]:
        """父会话的归属子查询，是消息操作的唯一过滤入口。"""
        return select(AIConversation.id).where(AIConversation.user_id == require_user_id("AIMessage", user_id))

    def _owned_condition(self, user_id: str | None) -> ColumnElement[bool]:
        """把归属条件作用在 `conversation_id` 上（消息表本身没有 user_id）。

        做成 ColumnElement 而不是整条 SELECT，是为了能直接拼进 `delete()`：
        子查询在删除语句内部求值，与会话行的删除同时生效，不留下"先查后删"的窗口。
        """
        return AIMessage.conversation_id.in_(self._owned_conversation_ids(user_id))

    async def list_by_conversation(self, db: AsyncSession, user_id: str, conv_id: str) -> Sequence[AIMessage]:
        """按会话取消息。不属于当前用户时返回空列表，不区分"会话不存在"。"""
        stmt = (
            select(AIMessage)
            .where(AIMessage.conversation_id == conv_id, self._owned_condition(user_id))
            .order_by(AIMessage.id)
        )
        return (await db.execute(stmt)).scalars().all()

    async def add_many(
        self, db: AsyncSession, user_id: str, conv_id: str, messages: Sequence[AIMessage]
    ) -> Sequence[AIMessage]:
        """写入消息前先确认父会话归属：INSERT 无法带 where 条件，只能先校验。

        校验只做一次（一轮对话固定写用户与 AI 两条），且必须在父会话 `flush` 之后
        调用——会话工厂是 `autoflush=False`，未 flush 的新会话不会出现在查询结果里。
        """
        uid = require_user_id("AIMessage", user_id)
        owned = (
            await db.execute(
                select(AIConversation.id).where(AIConversation.id == conv_id, AIConversation.user_id == uid)
            )
        ).first()
        if owned is None:
            raise OwnershipError("会话不存在或无权访问")
        for message in messages:
            message.conversation_id = conv_id
            db.add(message)
        return messages

    async def delete_by_conversation(self, db: AsyncSession, user_id: str, conv_id: str) -> int:
        stmt = delete(AIMessage).where(AIMessage.conversation_id == conv_id, self._owned_condition(user_id))
        return await execute_write(db, stmt)

    async def delete_by_user(self, db: AsyncSession, user_id: str) -> int:
        """删本人全部会话下的消息（注销账号用）。

        条件写成 `conversation_id IN (本人会话子查询)`，不依赖调用方先取出的 id 列表：
        现状 `user_service.cancel_account` 先查 `conv_ids` 再 `in_(conv_ids)`，若两步
        之间会话行被删而消息仍在，消息就成孤儿。子查询让判定与删除同时生效。
        """
        uid = require_user_id("AIMessage", user_id)
        stmt = delete(AIMessage).where(self._owned_condition(uid))
        return await execute_write(db, stmt)


message_repo = ConversationScopedRepository()

__all__ = ["ConversationScopedRepository", "message_repo"]
