from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class MemorySummary(Base, TimestampMixin):
    """跨会话记忆摘要表。"""

    __tablename__ = "memory_summary"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="会话摘要")
    key_facts: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="抽取的关键事实")
