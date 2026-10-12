"""建设备接入配置表与字段映射表（T5-01）

Revision ID: 0014_device_config_mapping
Revises: 0013_ledger_entry
Create Date: 2026-10-12

本迁移是 T5-01（设备配置表与字段映射表）的数据层改造，按技术方案 12.6 / 12.7 新增两张表：

1. **`device_config`**：设备接入配置。记录农户已绑定设备与接入参数（协议、厂商模板引用、
   绑定/解绑时刻、密钥引用与摘要）。解绑走软删除（写 `unbound_at`）而非物理删除，使
   T5-09 可复用历史映射，也使设备上报入口（T5-06）能靠 `unbound_at IS NULL` 定位在绑设备。
2. **`field_mapping`**：字段映射。把设备原始字段翻译成标准环境指标，与设备配置分离，使同品牌
   同型号设备可共用预置模板。外键 `field_mapping.device_id -> device_config.id` 带
   `ON DELETE CASCADE`（技术方案 12.7 列为中等优先级外键）。

两处与任务文件迁移片段的取舍，均为遵守本仓库既有约定：

- **不写 `protocol_type` / `scale_factor` / `offset_value` 的 `server_default`**。任务片段给了
  `server_default="http_direct"` / `"1"` / `"0"`，但本仓库对"有 Python 侧 `default=` 兜底"的列
  一律不写数据库默认值（见 `0001_baseline` 的 `total_mu` / `plant_mu` / `mu` 与 `device.status`）。
  写了会让 `compare_server_default=True` 在 ORM（无 server_default）与库（有）之间持续报假差异，
  `test_metadata_matches_orm` 会失败。
- **时间列两侧都写 `server_default=now()`**（`bound_at`），与全库时间口径一致。

`device_sn` 用唯一索引 `ix_device_config_device_sn` 承载唯一性（列级 `unique=True, index=True`），
不另加表级 `UniqueConstraint`，与既有 `device.device_sn` 的做法一致。列、索引、约束与
`app/models/device.py` 的 `DeviceConfig` / `FieldMapping` 逐字对齐。
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_device_config_mapping"
down_revision = "0013_ledger_entry"
branch_labels = None
depends_on = None

_OWNED_COMMENT = "归属用户ID"
_DEVICE_CONFIG_COMMENT = "设备接入配置：农户已绑定设备与接入参数"
_FIELD_MAPPING_COMMENT = "字段映射：设备原始字段到标准环境指标的翻译表"


def upgrade() -> None:
    op.create_table(
        "device_config",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        sa.Column("device_sn", sa.String(60), nullable=False, comment="设备序列号"),
        sa.Column("farm_entry_id", sa.String(36), nullable=True, comment="关联经营条目"),
        sa.Column("vendor_template_id", sa.String(36), nullable=True, comment="使用的厂商模板"),
        sa.Column("protocol_type", sa.String(20), nullable=False, comment="接入协议"),
        sa.Column("online_status", sa.String(20), nullable=True, comment="online/offline/fault"),
        sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=True, comment="最近一次上报时刻"),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="绑定时刻",
        ),
        sa.Column("unbound_at", sa.DateTime(timezone=True), nullable=True, comment="解绑时刻，空为在绑"),
        sa.Column("secret_ref", sa.String(120), nullable=True, comment="密钥托管引用，不存明文"),
        sa.Column("secret_digest", sa.String(64), nullable=True, comment="密钥加盐摘要，T5-03 写入"),
        comment=_DEVICE_CONFIG_COMMENT,
    )
    # OwnedMixin 自动带的 user_id 单列索引。
    op.create_index("ix_device_config_user_id", "device_config", ["user_id"])
    # 设备序列号唯一索引（列级 unique=True, index=True）。
    op.create_index("ix_device_config_device_sn", "device_config", ["device_sn"], unique=True)
    # 看板取"在绑设备"：条件固定为 user_id + unbound_at。
    op.create_index("ix_device_config_user_bound", "device_config", ["user_id", "unbound_at"])

    op.create_table(
        "field_mapping",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        sa.Column(
            "device_id",
            sa.String(36),
            sa.ForeignKey(
                "device_config.id",
                ondelete="CASCADE",
                name="field_mapping_device_id_fkey",
            ),
            nullable=False,
            comment="所属设备配置",
        ),
        sa.Column("raw_field", sa.String(40), nullable=False, comment='报文原始字段，如 "t1"'),
        sa.Column("standard_metric", sa.String(30), nullable=False, comment="标准环境指标取值"),
        sa.Column("unit", sa.String(10), nullable=True, comment="单位"),
        sa.Column("scale_factor", sa.Numeric(12, 6), nullable=False, comment="换算系数"),
        sa.Column("offset_value", sa.Numeric(12, 6), nullable=False, comment="换算偏移"),
        sa.Column("data_type", sa.String(10), nullable=True, comment="float/int/bool"),
        sa.Column("sample_raw", sa.Text(), nullable=True, comment="样例报文中的原始片段"),
        # 同一设备的同一原始字段只允许映射一次（"同一条读数写两遍"的根因防护）。
        sa.UniqueConstraint("device_id", "raw_field", name="uq_field_mapping_device_raw"),
        comment=_FIELD_MAPPING_COMMENT,
    )
    op.create_index("ix_field_mapping_user_id", "field_mapping", ["user_id"])
    op.create_index("ix_field_mapping_device", "field_mapping", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_field_mapping_device", table_name="field_mapping")
    op.drop_index("ix_field_mapping_user_id", table_name="field_mapping")
    op.drop_table("field_mapping")

    op.drop_index("ix_device_config_user_bound", table_name="device_config")
    op.drop_index("ix_device_config_device_sn", table_name="device_config")
    op.drop_index("ix_device_config_user_id", table_name="device_config")
    op.drop_table("device_config")
