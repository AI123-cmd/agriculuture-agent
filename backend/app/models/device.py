"""设备台账与传感器接入配置模型（T5-01，技术方案 12.6 / 12.7）。

三张表各司其职：

- `Device` 是设备台账（既有表，本次不被替换）。
- `DeviceConfig` 记录"这台设备怎么接进来"：协议、厂商模板引用、密钥引用与绑定关系。
- `FieldMapping` 记录"它的报文每一列是什么"：把设备原始字段翻译成系统标准环境指标。

后两者刻意分离（方案 12.6）：同品牌同型号设备报文结构一致，字段映射可作为模板复用到多台
设备；若把映射写进设备表，每台设备都要重复录入一份结构相同的配置，且一台多合一墒情站的
`t1`/`h1`/`ec1`/`ph1` 会让宽表退化成一列 JSON，索引与唯一约束都用不上。

三处与方案原文的取舍：

1. 时间列统一 `DateTime(timezone=True)`。方案写裸 `DateTime` 未标时区，而归属于新鲜度判断
   （T5-07 的 24 小时、T5-08 的 30 分钟）跨时区比较必须显式带时区，否则 `last_report_at`
   与 `observed_at` 相减会引入偏移。
2. `secret_ref` 与 `secret_digest` 一列超出方案 12.6 原文（来自调研报告 10.2 的"密钥存引用
   而非明文"）。`secret_ref` 只存密钥托管中的键名，`secret_digest` 存加盐摘要供常量时间比对，
   设备密钥明文不落任何表，写入时机在 T5-03。
3. `device_sn` 用列级 `unique=True, index=True`（落成唯一索引 `ix_device_config_device_sn`），
   不再另加表级 `UniqueConstraint`：两者都建会在同一列上出现唯一索引与唯一约束两份冗余，
   与既有 `device.device_sn` 的做法保持一致。

表结构由迁移 `0014_device_config_mapping` 显式建出，列、索引、约束必须与迁移逐字对齐，否则
`tests/test_migrations.py::test_metadata_matches_orm` 会报差异。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.owned import OwnedMixin


class Device(Base, TimestampMixin, OwnedMixin):
    """设备/传感器表。"""

    __tablename__ = "device"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # user_id 由 OwnedMixin 提供。原设计允许"未绑定为 null"，T1-05 收紧为
    # NOT NULL：解绑走软删除（T5-09 的 unbound_at），归属永远可判定。
    field_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    device_sn: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False, comment="设备序列号")
    device_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="soil/weather/camera")
    device_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="0离线1在线2故障")
    last_online: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bind_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceConfig(Base, OwnedMixin):
    """设备接入配置：记录农户已绑定设备与接入参数。

    `unbound_at` 为空表示在绑；解绑写 `unbound_at`（软删除）而非物理删除，使 T5-09 可复用
    历史映射，也使 `get_by_sn`（设备上报入口）能靠 `unbound_at IS NULL` 定位在绑设备。
    """

    __tablename__ = "device_config"
    __table_args__ = (
        # 看板按"在绑设备"取数，条件固定为 user_id + unbound_at。
        Index("ix_device_config_user_bound", "user_id", "unbound_at"),
        {"comment": "设备接入配置：农户已绑定设备与接入参数"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # user_id 由 OwnedMixin 提供，索引在 OwnedMixin 内建。
    device_sn: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False, comment="设备序列号")
    farm_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="关联经营条目")
    vendor_template_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="使用的厂商模板")
    protocol_type: Mapped[str] = mapped_column(String(20), nullable=False, default="http_direct", comment="接入协议")
    online_status: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="online/offline/fault")
    last_report_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次上报时刻"
    )
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="绑定时刻"
    )
    unbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="解绑时刻，空为在绑"
    )
    secret_ref: Mapped[str | None] = mapped_column(String(120), nullable=True, comment="密钥托管引用，不存明文")
    secret_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="密钥加盐摘要，T5-03 写入")


class FieldMapping(Base, OwnedMixin):
    """字段映射：把设备原始字段翻译成标准指标。

    与设备配置分离，使同品牌设备可共用预置模板。唯一约束 `(device_id, raw_field)` 由迁移显式
    建出，防止同一设备的同一原始字段被映射两次——那是"同一条读数写两遍"的根因。
    """

    __tablename__ = "field_mapping"
    __table_args__ = (
        UniqueConstraint("device_id", "raw_field", name="uq_field_mapping_device_raw"),
        # 按设备取整份映射（上报解析 T5-06 与映射保存 T5-05 都按 device_id 取全量）。
        Index("ix_field_mapping_device", "device_id"),
        {"comment": "字段映射：设备原始字段到标准环境指标的翻译表"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # user_id 由 OwnedMixin 提供。归属以本表 user_id 为唯一准据（写入时由
    # device_config.user_id 复制），读取一律带该条件，即使 device_id 被猜测也读不到他人数据。
    device_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("device_config.id", name="field_mapping_device_id_fkey", ondelete="CASCADE"),
        nullable=False,
        comment="所属设备配置",
    )
    raw_field: Mapped[str] = mapped_column(String(40), nullable=False, comment='报文原始字段，如 "t1"')
    standard_metric: Mapped[str] = mapped_column(String(30), nullable=False, comment="标准环境指标取值")
    unit: Mapped[str | None] = mapped_column(String(10), nullable=True, comment="单位")
    scale_factor: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=1, comment="换算系数")
    offset_value: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0, comment="换算偏移")
    data_type: Mapped[str | None] = mapped_column(String(10), nullable=True, comment="float/int/bool")
    sample_raw: Mapped[str | None] = mapped_column(Text, nullable=True, comment="样例报文中的原始片段")


__all__ = ["Device", "DeviceConfig", "FieldMapping"]
