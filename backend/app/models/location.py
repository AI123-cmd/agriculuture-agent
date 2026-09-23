from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Location(Base, TimestampMixin):
    """农事地点表。"""

    __tablename__ = "location"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    location_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="地点名称")
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", comment="manual/gps")
