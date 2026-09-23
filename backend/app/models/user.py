from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """用户表。"""

    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="用户ID")
    openid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False, comment="微信openid")
    unionid: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="微信unionid")
    nick_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="昵称")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="头像URL")
    gender: Mapped[int] = mapped_column(SmallInteger, default=0, comment="0未知1男2女")
    country: Mapped[str | None] = mapped_column(String(32), nullable=True)
    province: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(32), nullable=True)
