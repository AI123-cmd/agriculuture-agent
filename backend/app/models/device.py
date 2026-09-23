from datetime import datetime

from sqlalchemy import DateTime, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Device(Base, TimestampMixin):
    """设备/传感器表。"""

    __tablename__ = "device"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True, comment="未绑定为null")
    field_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    device_sn: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False, comment="设备序列号")
    device_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="soil/weather/camera")
    device_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="0离线1在线2故障")
    last_online: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bind_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
