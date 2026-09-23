"""环境读数时序模型（TimescaleDB 超表）。

表结构由迁移 `0002_env_reading` 用 DDL 建立（超表无法用 `create_all` 生成），
这里只做 ORM 映射，供后续 T5-07 读数落库与 T5-08 看板查询使用。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EnvReading(Base):
    """环境读数：按 `observed_at` 7 天一个 chunk 分区。

    主键是 `(id, observed_at)` 复合主键 —— TimescaleDB 要求分区列进入主键，
    单列 `id` 主键在超表上会直接报错。
    """

    __tablename__ = "env_reading"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    farm_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metric: Mapped[str] = mapped_column(String(30), nullable=False, comment="如 soil_temp / soil_moisture")
    value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, comment="sensor/weather/manual")
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="观测时刻，分区列"
    )
    quality: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="ok/suspect/stale：异常数据标记而非丢弃"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
