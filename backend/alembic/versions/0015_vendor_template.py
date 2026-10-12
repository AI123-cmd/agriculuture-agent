"""建厂商报文模板表（T5-02）

Revision ID: 0015_vendor_template
Revises: 0014_device_config_mapping
Create Date: 2026-10-12

本迁移是 T5-02（厂商模板库与内置模板）的数据层改造，按技术方案 12.6 新建 `vendor_template`：

- 模板是平台侧公共资产，**不带 `user_id`**（农户只能读不能写），这是全局约定"所有业务表带
  `user_id`"的合理例外。
- `field_spec` 用 `JSONB`（方案原文写裸 `JSON`）：需要按键查询与等值比较，与知识条目 `facts`
  列的选型一致。不写 `server_default`（本仓库对 JSONB 列的既有约定，避免假迁移）。
- `id` 用固定字符串（如 `tpl_generic_http`）而非随机 UUID，使内置模板在多环境间稳定对应。

内置模板数据不在此迁移中回填：由 `app/services/template_seed.py::seed_vendor_templates` 在
启动时幂等导入（与 `0004_process_config` 不回填、首次落库交给 `ConfigSyncService` 的口径一致）。
列与索引与 `app/models/vendor_template.py` 逐字对齐。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_vendor_template"
down_revision = "0014_device_config_mapping"
branch_labels = None
depends_on = None

_TABLE_COMMENT = "厂商报文模板：型号的字段预映射，使接入可跳过粘贴推断"


def upgrade() -> None:
    op.create_table(
        "vendor_template",
        sa.Column("id", sa.String(36), primary_key=True, comment="固定字符串 id，如 tpl_generic_http"),
        sa.Column("vendor_name", sa.String(60), nullable=False, comment="厂商/模板名"),
        sa.Column("model_pattern", sa.String(80), nullable=True, comment="型号匹配正则"),
        sa.Column("sample_payload", sa.Text(), nullable=False, comment="样例报文"),
        sa.Column("field_spec", postgresql.JSONB(), nullable=False, comment="字段 → 指标预映射"),
        sa.Column(
            "create_time",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "update_time",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="更新时间",
        ),
        comment=_TABLE_COMMENT,
    )
    # 按厂商名筛取（运营维护模板时按厂商分组查看）。
    op.create_index("ix_vendor_template_vendor", "vendor_template", ["vendor_name"])


def downgrade() -> None:
    op.drop_index("ix_vendor_template_vendor", table_name="vendor_template")
    op.drop_table("vendor_template")
