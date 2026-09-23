from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.owned import OwnedMixin


class AIConversation(Base, TimestampMixin, OwnedMixin):
    """AI 会话表（id 即 sessionId）。"""

    __tablename__ = "ai_conversation"
    # T1-11 的历史列表按 update_time 倒序分页，必须走索引
    __table_args__ = (Index("ix_ai_conversation_update_time", "update_time"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False, comment="对话标题")
    crop_tag: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="作物标签")


class AIMessage(Base, TimestampMixin):
    """AI 消息表。

    `id` 用 `BigInteger` 而非 `Integer`：消息表是增长最快的表，`Integer` 上限
    21 亿在 5 年内可能触及。
    """

    __tablename__ = "ai_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ai_conversation.id", name="fk_ai_message_conversation", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="user/ai")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
