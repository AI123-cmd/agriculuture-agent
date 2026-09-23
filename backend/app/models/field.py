from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Field(Base, TimestampMixin):
    """地块表。"""

    __tablename__ = "field"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="地块名称")
    mu: Mapped[float] = mapped_column(Numeric(8, 2), default=0, comment="面积(亩)")
    soil_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="土壤类型")
    soil_ph: Mapped[float | None] = mapped_column(Numeric(3, 1), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
