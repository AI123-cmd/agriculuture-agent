"""`ai_message` 的子表仓储：归属经父会话间接判定。

`ai_message` 没有 `user_id` 列（它经 `conversation_id` 归属），因此进不了
`OwnedRepository`。所有读写都以"父会话属于当前用户"为唯一过滤入口，用子查询写
条件而不是先查父表再判断——先查后判会在两步之间留下竞态窗口，子查询则与语句同时
生效。
"""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Subquery

from app.core.ownership import OwnershipError
from app.models import AIConversation, AIMessage
from app.repositories.base import execute_write, require_user_id

# 历史列表的预览长度。截断放在 SQL 侧：`content` 是 Text 列，整段取回只为渲染
# 一行摘要，正是产品说明书 9.2 第 7 条说的浪费。
_PREVIEW_CHARS = 30


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

    def preview_expr(self) -> ColumnElement[str]:
        """最后一条消息的预览片段（在 SQL 侧截断，见 `_PREVIEW_CHARS`）。"""
        return func.left(AIMessage.content, _PREVIEW_CHARS)

    def last_contents_by_conversation(self) -> Subquery:
        """每条会话的"消息数 + 最后一条消息预览"，供历史列表一次取回。

        `COUNT(*) OVER (PARTITION BY ...)` 与 `ROW_NUMBER() ... = 1` 共用同一遍扫描：
        前者顺带算出消息数，后者取每组最后一条，因此不需要第二条聚合查询。

        预览用 `left(content, 30)` 在库侧截断：`content` 是 Text，整段取回只为渲染
        一行摘要，正是产品说明书 9.2 第 7 条指出的浪费。

        返回的是子查询而非结果：调用方要与 `ai_conversation` 做 `LEFT JOIN` 分页，
        必须留在同一条 SQL 里才不产生第二条查询。归属过滤由调用方的 JOIN 条件完成
        ——子查询只按 `conversation_id` 分组，不引入额外的跨用户读取。
        """
        ranked = select(
            AIMessage.conversation_id.label("conversation_id"),
            self.preview_expr().label("content"),
            func.count().over(partition_by=AIMessage.conversation_id).label("msg_count"),
            func.row_number().over(partition_by=AIMessage.conversation_id, order_by=AIMessage.id.desc()).label("rn"),
        ).subquery("ranked")
        return (
            select(ranked.c.conversation_id, ranked.c.content, ranked.c.msg_count)
            .where(ranked.c.rn == 1)
            .subquery("agg")
        )

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
