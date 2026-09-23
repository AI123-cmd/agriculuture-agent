from datetime import date

from sqlalchemy import Date, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Crop(Base, TimestampMixin):
    """作物表。"""

    __tablename__ = "crop"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    field_id: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="关联地块ID")
    crop_name: Mapped[str] = mapped_column(String(32), nullable=False, comment="作物名称(用户自由输入)")
    crop_variety: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="品种")
    sowing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    growth_stage: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="生长阶段")
    plant_mu: Mapped[float] = mapped_column(Numeric(8, 2), default=0, comment="种植面积(亩)")
    is_main: Mapped[int] = mapped_column(SmallInteger, default=0, comment="1主作物0否")
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment="1在种0已收获")
    harvest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
