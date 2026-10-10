"""建农事台账表、补处方卡三列、补快照流程列、废弃 broadcast_log（T3-15）

Revision ID: 0013_ledger_entry
Revises: 0012_prescription_card_followup
Create Date: 2026-10-08

本迁移是 T3-15（处方卡台账与执行回执）的数据层改造，含四件事：

1. **新建 `ledger_entry`**：农事台账条目。幂等键 `(user_id, source_type, source_id)` 用
   显式唯一约束 `uq_ledger_entry_source` 建立——农户会重复点击"存入台账"，没有它同一份
   处方会在台账里出现多条重复记录。列与索引与 ORM `app/models/ledger.py` 逐字对齐。
2. **补 `prescription_card` 三列**：`created_at`（技术方案 12.4 已给，入台账时取
   `entry_date`）、`process_id` / `diagnosis_summary`（12.4 骨架未给，按"逻辑以新文档为主"
   就地补列）。`created_at` 走 `server_default=now()`，与全库时间口径一致；另两列放开可空。
3. **补 `prompt_snapshot.process_id`**：执行率需按流程分别统计（产品说明书 6.3），而
   `prompt_item` 无流程列，统计只能经快照的 `snapshot_id` 反查；本列可空以兼容历史快照。
4. **废弃 `broadcast_log`**：旧的 `BroadcastLog` 整体废弃（T3-15 第五节），`done` 是
   0/1 整数标记，无法表达取消后的时间戳清理、无法关联 `prompt_item`、也没有"被抑制条目
   不接受勾选"的约束；其展示出参已由 T3-10 的 `SnapshotView` 取代。`downgrade` 按
   `0001_baseline` 的定义重建该表，保证迁移可逆。

迁移不写 JSONB/Boolean 列的 `server_default`（由 Python 侧 `default=` 兜底），这是本仓库
对这两类列的既有约定，避免 `compare_server_default=True` 持续生成假迁移；`created_at`
是时间列，两侧都写 `server_default=now()`。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_ledger_entry"
down_revision = "0012_prescription_card_followup"
branch_labels = None
depends_on = None

_LEDGER_TABLE_COMMENT = "农事台账：处方卡等沉淀的农事记录，按 entry_date 排列"
_OWNED_COMMENT = "归属用户ID"


def upgrade() -> None:
    op.create_table(
        "ledger_entry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        sa.Column("entry_date", sa.Date(), nullable=False, comment="农事发生日"),
        sa.Column("process_id", sa.String(20), nullable=True, comment="七条流程之一"),
        sa.Column("source_type", sa.String(20), nullable=False, comment="来源类型，如 prescription"),
        sa.Column("source_id", sa.String(36), nullable=False, comment="来源主键，处方的 card.id"),
        sa.Column("title", sa.String(120), nullable=True, comment="标题，取处方诊断摘要"),
        sa.Column("actions", postgresql.JSONB(), nullable=False, comment="处置动作快照"),
        sa.Column("pesticides", postgresql.JSONB(), nullable=False, comment="药剂条目快照（名/剂量）"),
        sa.Column("safety_interval_days", sa.Integer(), nullable=True, comment="安全间隔期（天）"),
        sa.Column("facts_provenance", postgresql.JSONB(), nullable=False, comment="数值溯源表"),
        sa.Column("followup_needed", sa.Boolean(), nullable=False, comment="是否登记了回访"),
        # 幂等键：同一来源在同一农户下只允许一条台账记录。
        sa.UniqueConstraint("user_id", "source_type", "source_id", name="uq_ledger_entry_source"),
        comment=_LEDGER_TABLE_COMMENT,
    )
    # OwnedMixin 自动带的 user_id 单列索引。
    op.create_index("ix_ledger_entry_user_id", "ledger_entry", ["user_id"])
    # 台账列表：按"用户 + 农事日期"倒序取数（无生育期流程的纯时间线亦然）。
    op.create_index("ix_ledger_entry_user_date", "ledger_entry", ["user_id", "entry_date"])

    # 处方卡补列：created_at 走 server_default=now()（时间列两侧都写），另两列可空。
    # created_at 语义必须（非 Optional Mapped），故 NOT NULL；已有行由 server_default 回填。
    op.add_column(
        "prescription_card",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
    )
    op.add_column(
        "prescription_card",
        sa.Column("process_id", sa.String(20), nullable=True, comment="七条流程之一"),
    )
    op.add_column(
        "prescription_card",
        sa.Column("diagnosis_summary", sa.Text(), nullable=True, comment="诊断摘要，来自处方契约"),
    )

    # 快照补流程列：执行率按流程分别统计的维度来源。
    op.add_column(
        "prompt_snapshot",
        sa.Column("process_id", sa.String(20), nullable=True, comment="本次生成所用的流程"),
    )

    # 废弃旧的播报日志表：旧接口与旧字段整体废弃，不留兼容路由（T3-15 第五节）。
    op.drop_index("ix_broadcast_log_date", table_name="broadcast_log")
    op.drop_index("ix_broadcast_log_user_id", table_name="broadcast_log")
    op.drop_table("broadcast_log")


def downgrade() -> None:
    # 重建 broadcast_log：列、索引、注释与 0001_baseline 的定义逐字一致，保证可逆。
    op.create_table(
        "broadcast_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        sa.Column("date", sa.String(16), nullable=False, comment="YYYY-MM-DD"),
        sa.Column("time", sa.String(8), nullable=True),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("content", sa.String(500), nullable=False),
        sa.Column("type", sa.String(32), nullable=False, comment="fertilize/pest/water/info"),
        sa.Column("level", sa.String(10), nullable=False, comment="danger/warning/normal"),
        sa.Column("source", sa.String(20), nullable=False, comment="sensor/weather/calendar/ai"),
        sa.Column("meta", postgresql.JSONB(), nullable=True, comment="触发该条播报的环境快照"),
        sa.Column("done", sa.SmallInteger(), nullable=False, comment="0未完成1已完成"),
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
    )
    op.create_index("ix_broadcast_log_user_id", "broadcast_log", ["user_id"])
    op.create_index("ix_broadcast_log_date", "broadcast_log", ["date"])

    # 逆序撤销本迁移新增的列与表。
    op.drop_column("prompt_snapshot", "process_id")
    op.drop_column("prescription_card", "diagnosis_summary")
    op.drop_column("prescription_card", "process_id")
    op.drop_column("prescription_card", "created_at")
    op.drop_table("ledger_entry")
