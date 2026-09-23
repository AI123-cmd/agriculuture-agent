from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class MemorySummary(Base, TimestampMixin):
    """跨会话记忆摘要表。"""

    __tablename__ = "memory_summary"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="会话摘要")
    key_facts: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="抽取的关键事实")
