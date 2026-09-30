"""建问诊会话与处方卡三张表（T3-04 前置，技术方案 12.4）

Revision ID: 0010_consultation
Revises: 0009_prompt_snapshot
Create Date: 2026-09-30

`consultation` / `prescription_card` / `followup_task` 三张表本应由 T1-04 的基线迁移
建出（技术方案 12.4 有定义），实际缺失。T3-04（问诊与回访信号采集）读取
`consultation.status == 'observing'` 的会话与 `followup_task` 的到期任务，故按 12.4
的定义就地补齐。链的 head 是 `0009_prompt_snapshot`，本文件接在其后。

表结构由本迁移显式建出，ORM 侧 `app/models/consultation.py` 只做映射，两者的列、
索引、外键、表注释必须逐字对齐，否则 `test_metadata_matches_orm` 会以非空 diff 报出。

三处与 12.4 骨架的落地方式：

1. JSON 列一律用 `JSONB`（与 `0005_gate_audit`、`0009_prompt_snapshot` 及全库既有
   JSON 列一致），而非 12.4 原文的裸 `JSON`。
2. 迁移不写服务端默认值（`server_default`），由 Python 侧的 `default=` 兜底：这是
   本仓库对 JSONB/Boolean 列的既有约定，避免 `compare_server_default=True` 持续生成
   "删除默认值"的假迁移。`create_time` / `update_time` 的 `server_default=now()` 是
   全局统一的时间口径，两侧都写。
3. `status` / `kind` 用 `String` 列而非 PostgreSQL 原生 ENUM，取值闭集由 ORM 的
   `@validates` 强制（全局约定第四节），新增取值只改代码不动表。

三条高优先级外键（技术方案 12.7）用 `op.create_foreign_key` 显式命名，`ondelete`
保持默认 `RESTRICT`——处方与回访是审计链路，不允许级联删除。`downgrade` 按逆依赖序
drop（子表在前，否则外键阻塞 DROP）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_consultation"
down_revision = "0009_prompt_snapshot"
branch_labels = None
depends_on = None

_CONSULTATION_TABLE_COMMENT = "问诊会话：一次图驱动问诊一行，id 即 session_id"
_CARD_TABLE_COMMENT = "处方卡：问诊产出的处方快照与闸门结论，含回访计划"
_FOLLOWUP_TABLE_COMMENT = "回访任务：处方卡登记的回访时点与结果，首页提示的数据来源"

_OWNED_COMMENT = "归属用户ID"


def upgrade() -> None:
    op.create_table(
        "consultation",
        sa.Column("id", sa.String(36), primary_key=True, comment="= session_id"),
        sa.Column("farm_entry_id", sa.String(36), nullable=True, comment="关联经营条目"),
        sa.Column("process_id", sa.String(20), nullable=False, comment="七条流程之一"),
        sa.Column("process_locked", sa.Boolean(), nullable=False, comment="流程是否锁定"),
        sa.Column("title", sa.String(60), nullable=False, comment="会话标题"),
        sa.Column("stage", sa.String(20), nullable=False, comment="当前阶段"),
        sa.Column("rounds", sa.Integer(), nullable=False, comment="追问轮次"),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            comment="observing/resolved/followup/escalated/blocked",
        ),
        sa.Column("triage_snapshot", postgresql.JSONB(), nullable=False, comment="分诊结果快照"),
        sa.Column("slots_snapshot", postgresql.JSONB(), nullable=False, comment="最终槽位快照"),
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
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        comment=_CONSULTATION_TABLE_COMMENT,
    )
    # OwnedMixin 自动带的 user_id 单列索引。
    op.create_index("ix_consultation_user_id", "consultation", ["user_id"])
    # 首页取"观察期会话"与历史列表按用户 + 创建时间取数。
    op.create_index("ix_consultation_user_created", "consultation", ["user_id", "create_time"])
    # 按状态筛取（T3-04 的 list_observing）。
    op.create_index("ix_consultation_user_status", "consultation", ["user_id", "status"])

    op.create_table(
        "prescription_card",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("consultation_id", sa.String(36), nullable=False, comment="所属问诊"),
        sa.Column("diagnosis", postgresql.JSONB(), nullable=False, comment="诊断结果"),
        sa.Column("actions", postgresql.JSONB(), nullable=False, comment="处置动作列表"),
        sa.Column("pesticide_items", postgresql.JSONB(), nullable=False, comment="药剂条目列表"),
        sa.Column("safety_interval_days", sa.Integer(), nullable=True, comment="安全间隔期（天）"),
        sa.Column("max_uses_per_season", sa.Integer(), nullable=True, comment="每季使用上限"),
        sa.Column("facts_provenance", postgresql.JSONB(), nullable=False, comment="数值溯源表"),
        sa.Column("gate_result", postgresql.JSONB(), nullable=False, comment="闸门结论"),
        sa.Column("gate_config_version", sa.Integer(), nullable=False, comment="闸门配置版本"),
        sa.Column("requires_tech_station", sa.Boolean(), nullable=False, comment="是否需转农技站"),
        sa.Column("ledger_saved", sa.Boolean(), nullable=False, comment="是否已存入台账"),
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        comment=_CARD_TABLE_COMMENT,
    )
    op.create_index("ix_prescription_card_user_id", "prescription_card", ["user_id"])
    # 按问诊取处方卡：一次问诊的处方详情与回访登记都要按 consultation_id 反查。
    op.create_index("ix_prescription_card_consultation_id", "prescription_card", ["consultation_id"])

    op.create_table(
        "followup_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("consultation_id", sa.String(36), nullable=False, comment="所属问诊"),
        sa.Column("prescription_card_id", sa.String(36), nullable=False, comment="对应处方卡"),
        sa.Column(
            "kind",
            sa.String(30),
            nullable=False,
            comment="effect_check/risk_check/preserve_scene/observe/compliance_check",
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False, comment="到期时点"),
        sa.Column("status", sa.String(20), nullable=False, comment="pending/done/skipped"),
        sa.Column("result", sa.String(20), nullable=True, comment="improved/unchanged/worse"),
        sa.Column("result_note", sa.Text(), nullable=True, comment="回填说明"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True, comment="关闭时点"),
        sa.Column("user_id", sa.String(36), nullable=False, comment=_OWNED_COMMENT),
        comment=_FOLLOWUP_TABLE_COMMENT,
    )
    op.create_index("ix_followup_task_user_id", "followup_task", ["user_id"])
    # 到期扫描主索引：`status='pending' AND due_at <= now()`（T3-04 的 list_due / T3-09 的扫描）。
    op.create_index("ix_followup_task_status_due", "followup_task", ["status", "due_at"])
    # 按问诊与按处方卡反查。
    op.create_index("ix_followup_task_consultation_id", "followup_task", ["consultation_id"])
    op.create_index("ix_followup_task_prescription_card_id", "followup_task", ["prescription_card_id"])

    # 三条高优先级外键（技术方案 12.7）。`ondelete` 保持默认 RESTRICT，显式命名以与
    # ORM 的 ForeignKey(name=...) 逐字对齐。
    op.create_foreign_key(
        "fk_prescription_card_consultation",
        "prescription_card",
        "consultation",
        ["consultation_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_followup_task_consultation",
        "followup_task",
        "consultation",
        ["consultation_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_followup_task_prescription_card",
        "followup_task",
        "prescription_card",
        ["prescription_card_id"],
        ["id"],
    )


def downgrade() -> None:
    # 逆依赖序：先 drop 引用者（followup_task 引用两张父表），再 drop 被引用者。
    op.drop_table("followup_task")
    op.drop_table("prescription_card")
    op.drop_table("consultation")
