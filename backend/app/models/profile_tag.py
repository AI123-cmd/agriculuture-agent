from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.owned import OwnedMixin


class UserProfileTag(Base, TimestampMixin, OwnedMixin):
    """用户画像与 AI 上下文表。"""

    __tablename__ = "user_profile_tag"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 覆盖 OwnedMixin 的列：一个用户只有一份画像，需唯一约束
    user_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False, comment="归属用户ID")
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="画像标签")
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="AI上下文快照")
    last_chat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chat_count: Mapped[int] = mapped_column(Integer, default=0)
