"""ORM 基类与通用工具。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def new_id(prefix: str) -> str:
    """生成业务主键，形如 user_<24位hex>（29 字符，落在 String(36) 内）。"""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class TimestampMixin:
    """时间戳混入：统一带时区，交由数据库生成默认值。

    裸 `DateTime` 在 PostgreSQL 下会落成 `timestamp without time zone`，
    与全局约定"时间统一带时区"冲突，故显式声明 `timezone=True`。
    """

    create_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


class UUIDPrimaryKeyMixin:
    """主键混入：String(36) 存 uuid4().hex。"""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
