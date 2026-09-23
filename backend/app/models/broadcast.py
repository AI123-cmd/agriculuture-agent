from sqlalchemy import JSON, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class BroadcastLog(Base, TimestampMixin):
    """今日农事播报日志表。"""

    __tablename__ = "broadcast_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    date: Mapped[str] = mapped_column(String(16), index=True, nullable=False, comment="YYYY-MM-DD")
    time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="info", comment="fertilize/pest/water/info")
    level: Mapped[str] = mapped_column(String(10), default="normal", nullable=False, comment="danger/warning/normal")
    source: Mapped[str] = mapped_column(
        String(20), default="sensor", nullable=False, comment="sensor/weather/calendar/ai"
    )
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="触发该条播报的环境快照")
    done: Mapped[int] = mapped_column(SmallInteger, default=0, comment="0未完成1已完成")
