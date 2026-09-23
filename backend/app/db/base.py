"""ORM 基类与通用工具。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def new_id(prefix: str) -> str:
    """生成业务主键，形如 user_<24位hex>。"""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class TimestampMixin:
    create_time: Mapped[datetime] = mapped_column(DateTime, default=func.now(), comment="创建时间")
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), comment="更新时间"
    )