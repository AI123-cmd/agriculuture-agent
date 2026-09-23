from sqlalchemy import ForeignKey, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class FarmerProfile(Base, TimestampMixin):
    """农户档案表。"""

    __tablename__ = "farmer_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user.id", name="fk_farmer_profile_user", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    real_name: Mapped[str] = mapped_column(String(32), nullable=False, comment="真实姓名")
    age: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    gender: Mapped[int] = mapped_column(SmallInteger, default=0, comment="1男2女")
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    experience_year: Mapped[int] = mapped_column(SmallInteger, default=0, comment="种植经验年限")
    province: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(32), nullable=True)
    district: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_mu: Mapped[float] = mapped_column(Numeric(8, 2), default=0, comment="总种植面积(亩)")
    cultivation_type: Mapped[int] = mapped_column(SmallInteger, default=0, comment="1露地2大棚3混合")
    water_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
