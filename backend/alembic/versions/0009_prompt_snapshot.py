"""建提示与触达四张表（T3-01，技术方案 12.5）

Revision ID: 0009_prompt_snapshot
Revises: 0008_llm_call
Create Date: 2026-09-30

任务文件（T3-01）里写的编号是 `0004_prompt_snapshot`，但该编号在 T1-04 完成后已被
`0004_process_config` 占用，链的实际 head 是 `0008_llm_call`。新迁移只能接在其后，
故本文件编号为 `0009_prompt_snapshot`；`app/core/startup_check.py` 的 `EXPECTED_REVISION`
同步改为本编号，否则启动自检与 `check_migrations.sh` 都会失败（沿用 `0006_knowledge`
与 `0008_llm_call` 的既有处理）。

四张表（`prompt_snapshot` / `prompt_item` / `subscription_quota` / `reach_record`）是
T3-02 至 T3-15 全部代码的落点。表结构由本迁移显式建出，ORM 侧 `app/models/snapshot.py`
与 `app/models/reach_record.py` 只做映射，两者的列、索引、唯一约束、表注释必须逐字对齐，
否则 `test_metadata_matches_orm` 会以非空 diff 报出。

两处与任务骨架不同的落地方式：

1. `facts_provenance` 用 `JSONB` 而非方案原文的 `JSON`，与全库既有 JSON 列一致
   （沿用 `0005_gate_audit` 与 T5-02 的既有偏离）。
2. 十二条索引中有两条是"部分索引"，用原始 DDL 建：部分唯一索引
   `uq_prompt_snapshot_daily_no_farm`（`farm_entry_id IS NULL`，为杂草流程补唯一约束对
   NULL 不去重的缺口）与部分索引 `ix_prompt_item_unsuppressed`（`suppressed = false`）。
   与 `0003_event_outbox` 的 `ix_event_outbox_pending` 同一处理方式：写成 DDL 更直白，
   且与 `pg_indexes` 里的最终形态一致。

两条高优先级外键（技术方案 12.7）用 `op.create_foreign_key` 显式命名：`ondelete` 保持
默认（`RESTRICT`），快照与提示条目不允许级联删除，触达记录是送达率指标的原始数据，
删除会污染统计口径。`downgrade` 按逆依赖序 drop（子表在前，否则外键阻塞 DROP）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_prompt_snapshot"
down_revision = "0008_llm_call"
branch_labels = None
depends_on = None

_SNAPSHOT_TABLE_COMMENT = "提示快照：一次生成的结果容器，幂等键=(user_id, farm_entry_id, snapshot_date, revision)"
_ITEM_TABLE_COMMENT = "提示条目：快照内的单条五段式提示，含被条数控制压制的项"
_QUOTA_TABLE_COMMENT = "订阅额度账本：一户一行，remaining 冗余存储以支持无锁原子扣减"
_REACH_TABLE_COMMENT = "触达记录：每次触达尝试一行，含失败原因分类，是送达率指标的原始数据"


def upgrade() -> None:
    op.create_table(
        "prompt_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment="归属用户ID"),
        sa.Column("farm_entry_id", sa.String(36), nullable=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("env_source", sa.String(20), nullable=False, comment="sensor/sensor_stale/weather/none"),
        sa.Column("revision", sa.Integer(), nullable=False, comment="同日第几次更新"),
        sa.Column("invalidated", sa.Boolean(), nullable=False, comment="逻辑失效标记，不物理删除"),
        # 幂等键的唯一约束：并发写入同一日的两条快照会同时存在，幂等检查在提交后才生效就来不及了。
        sa.UniqueConstraint(
            "user_id", "farm_entry_id", "snapshot_date", "revision", name="uq_prompt_snapshot_daily"
        ),
        comment=_SNAPSHOT_TABLE_COMMENT,
    )
    # OwnedMixin 自动带的 user_id 单列索引（全局约定"所有业务表带 user_id，索引必建"）。
    op.create_index("ix_prompt_snapshot_user_id", "prompt_snapshot", ["user_id"])
    # 幂等键与列表查询的主索引：按"用户 + 自然日"取当天快照。
    op.create_index("ix_prompt_snapshot_user_date", "prompt_snapshot", ["user_id", "snapshot_date"])
    # 带地块的幂等键：同日同地块只应有一份有效快照。
    op.create_index(
        "ix_prompt_snapshot_user_farm_date", "prompt_snapshot", ["user_id", "farm_entry_id", "snapshot_date"]
    )
    # 部分唯一索引：唯一约束对 NULL 不去重，杂草流程（farm_entry_id IS NULL）的同日同版本
    # 唯一性只能靠这条部分索引补上。
    op.execute(
        "CREATE UNIQUE INDEX uq_prompt_snapshot_daily_no_farm "
        "ON prompt_snapshot (user_id, snapshot_date, revision) WHERE farm_entry_id IS NULL"
    )

    op.create_table(
        "prompt_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment="归属用户ID"),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False, comment="env_risk/calendar/followup"),
        sa.Column("severity", sa.String(10), nullable=False, comment="urgent/notice/reminder"),
        sa.Column("callout", sa.String(30), nullable=False, comment="呼告（对象）"),
        sa.Column("fact", sa.Text(), nullable=False, comment="事实"),
        sa.Column("impact", sa.Text(), nullable=False, comment="影响"),
        sa.Column("action", sa.Text(), nullable=False, comment="行动"),
        sa.Column("facts_provenance", postgresql.JSONB(), nullable=False, comment="事实来源映射"),
        sa.Column("rule_id", sa.String(40), nullable=False),
        sa.Column("priority_score", sa.Numeric(8, 3), nullable=False, comment="排序键，三维拼接为稳定分数"),
        sa.Column("suppressed", sa.Boolean(), nullable=False, comment="被条数控制截断"),
        sa.Column("executed", sa.Boolean(), nullable=False, comment="执行勾选（北极星指标）"),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        comment=_ITEM_TABLE_COMMENT,
    )
    op.create_index("ix_prompt_item_user_id", "prompt_item", ["user_id"])
    # 按快照取条目：视图组装与展示列表的主查询。
    op.create_index("ix_prompt_item_snapshot_id", "prompt_item", ["snapshot_id"])
    # 部分索引：只含未被压制的行，供执行率统计使用。
    op.execute("CREATE INDEX ix_prompt_item_unsuppressed ON prompt_item (snapshot_id) WHERE suppressed = false")

    op.create_table(
        "subscription_quota",
        sa.Column("user_id", sa.String(36), primary_key=True, comment="归属用户ID"),
        sa.Column("granted_total", sa.Integer(), nullable=False, comment="累计授予额度"),
        sa.Column("consumed_total", sa.Integer(), nullable=False, comment="累计消费额度"),
        sa.Column("remaining", sa.Integer(), nullable=False, comment="剩余额度（冗余，原子扣减）"),
        sa.Column("last_grant_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_consume_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_streak", sa.Integer(), nullable=False, comment="连续拒绝次数，用于降级索要"),
        comment=_QUOTA_TABLE_COMMENT,
    )

    op.create_table(
        "reach_record",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, comment="归属用户ID"),
        sa.Column("prompt_item_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, comment="subscribe_once/in_app"),
        sa.Column("status", sa.String(20), nullable=False, comment="delivered/failed"),
        sa.Column(
            "fail_reason", sa.String(30), nullable=True, comment="no_grant/quota_exhausted/api_error/quiet_hours"
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        comment=_REACH_TABLE_COMMENT,
    )
    op.create_index("ix_reach_record_user_id", "reach_record", ["user_id"])
    op.create_index("ix_reach_record_prompt_item_id", "reach_record", ["prompt_item_id"])
    op.create_index("ix_reach_record_user_attempted", "reach_record", ["user_id", "attempted_at"])

    # 两条高优先级外键（技术方案 12.7）。`ondelete` 保持默认 RESTRICT，显式命名以与
    # ORM 的 ForeignKey(name=...) 逐字对齐。
    op.create_foreign_key(
        "fk_prompt_item_snapshot",
        "prompt_item",
        "prompt_snapshot",
        ["snapshot_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_reach_record_prompt_item",
        "reach_record",
        "prompt_item",
        ["prompt_item_id"],
        ["id"],
    )


def downgrade() -> None:
    # 逆依赖序：先解除外键引用（子表在前），再 drop 表。索引随表一起删。
    op.drop_table("reach_record")
    op.drop_table("subscription_quota")
    op.drop_table("prompt_item")
    op.drop_table("prompt_snapshot")
