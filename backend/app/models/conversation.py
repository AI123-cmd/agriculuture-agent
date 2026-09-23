from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AIConversation(Base, TimestampMixin):
    """AI 会话表（id 即 sessionId）。"""

    __tablename__ = "ai_conversation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False, comment="对话标题")
    crop_tag: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="作物标签")


class AIMessage(Base, TimestampMixin):
    """AI 消息表。

    `id` 用 `BigInteger` 而非 `Integer`：消息表是增长最快的表，`Integer` 上限
    21 亿在 5 年内可能触及。
    """

    __tablename__ = "ai_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="user/ai")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
