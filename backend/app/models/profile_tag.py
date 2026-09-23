from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserProfileTag(Base, TimestampMixin):
    """用户画像与 AI 上下文表。"""

    __tablename__ = "user_profile_tag"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="画像标签")
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="AI上下文快照")
    last_chat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    chat_count: Mapped[int] = mapped_column(Integer, default=0)
