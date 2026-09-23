"""会话与消息仓储：归属校验 + 子表级联删除。

`ai_message` 没有 `user_id` 列，无法进 `OwnedRepository`（其泛型约束要求模型带
`user_id`）。它的归属只能经父会话 `conversation_id` 间接判定，故单独给
`ConversationScopedRepository`：每个方法第一步都先校验父会话归属。
"""

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import OwnershipError
from app.models import AIConversation, AIMessage
from app.repositories.base import OwnedRepository, execute_write, require_user_id


class ConversationRepository(OwnedRepository[AIConversation]):
    def __init__(self) -> None:
        super().__init__(AIConversation)

    async def delete_with_messages(self, db: AsyncSession, user_id: str, conv_id: str) -> int:
        """先校验归属，再删子表，最后删父表。

        固定这个顺序，是因为现状 `ai_service.delete_history` 只按 `conversation_id`
        删消息、不校验归属；一旦"删了消息但删会话失败"，消息就成了孤儿。
        两处删除都在调用方的同一事务内，由 T1-09 保证回滚。
        """
        if await self.get(db, user_id, conv_id) is None:
            raise OwnershipError("会话不存在或无权访问")
        await db.execute(delete(AIMessage).where(AIMessage.conversation_id == conv_id))
        return await self.delete(db, user_id, conv_id)


class ConversationScopedRepository:
    """会话子表（`ai_message`）仓储：先校验父会话归属，再操作消息。"""

    model: type[AIMessage] = AIMessage

    def __init__(self, conversations: ConversationRepository | None = None) -> None:
        self.conversations = conversations or conversation_repo

    async def _require_owned_conversation(self, db: AsyncSession, user_id: str, conv_id: str) -> None:
        if await self.conversations.get(db, user_id, conv_id) is None:
            raise OwnershipError("会话不存在或无权访问")

    async def list_by_conversation(self, db: AsyncSession, user_id: str, conv_id: str) -> Sequence[AIMessage]:
        await self._require_owned_conversation(db, user_id, conv_id)
        stmt = select(AIMessage).where(AIMessage.conversation_id == conv_id).order_by(AIMessage.id)
        return (await db.execute(stmt)).scalars().all()

    async def add(self, db: AsyncSession, user_id: str, conv_id: str, message: AIMessage) -> AIMessage:
        await self._require_owned_conversation(db, user_id, conv_id)
        message.conversation_id = conv_id
        db.add(message)
        return message

    async def delete_by_conversation(self, db: AsyncSession, user_id: str, conv_id: str) -> int:
        await self._require_owned_conversation(db, user_id, conv_id)
        stmt = delete(AIMessage).where(AIMessage.conversation_id == conv_id)
        return await execute_write(db, stmt)

    async def delete_by_user(self, db: AsyncSession, user_id: str) -> int:
        """按归属删除本人全部消息（注销账号用）。

        不依赖"先查会话 id 列表再删"的两步形态：那一步在并发下可能拿到空列表
        而漏删已存在的消息，留下孤儿。这里把归属条件直接写进 IN 子查询。
        """
        uid = require_user_id(self.model.__name__, user_id)
        owned_ids = select(AIConversation.id).where(AIConversation.user_id == uid)
        stmt = delete(AIMessage).where(AIMessage.conversation_id.in_(owned_ids))
        return await execute_write(db, stmt)


conversation_repo = ConversationRepository()
message_repo = ConversationScopedRepository(conversation_repo)
