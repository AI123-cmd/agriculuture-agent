"""建 llm_call 模型调用记录表（T2-59，技术方案 10.1 计量与审计）

Revision ID: 0008_llm_call
Revises: 0007_knowledge_review
Create Date: 2026-09-30

任务文件（T2-59）里写的编号是 `0005_llm_call`，但该编号在 T1-04 完成后已被
`0005_gate_audit` 占用，链的实际 head 是 `0007_knowledge_review`。新迁移只能接在其后，
故本文件编号为 `0008_llm_call`；`app/core/startup_check.py` 的 `EXPECTED_REVISION`
同步改为本编号，否则启动自检与 `check_migrations.sh` 都会失败。

表结构由本迁移显式建出，ORM 侧 `app/models/llm_call.py` 只做映射，两者的列与索引必须
逐字对齐，否则 `test_metadata_matches_orm` 会以非空 diff 报出。主键用 `BigInteger`
自增（BIGSERIAL）：高写入量记录表用整数主键，索引更紧凑，且归属由 `user_id` 表达。

`downgrade` 直接 drop 表（索引随表一起删）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_llm_call"
down_revision = "0007_knowledge_review"
branch_labels = None
depends_on = None

_TABLE_COMMENT = "模型调用记录：只追加，用于成本归属与版本对比"


def upgrade() -> None:
    op.create_table(
        "llm_call",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(36), nullable=True, comment="归属用户ID，匿名调用可为空"),
        sa.Column("consultation_id", sa.String(36), nullable=True),
        sa.Column("process_id", sa.String(20), nullable=True),
        sa.Column("agent", sa.String(30), nullable=False, comment="智能体名"),
        sa.Column("task_type", sa.String(30), nullable=False),
        sa.Column("tier", sa.String(10), nullable=False, comment="模型档位"),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(60), nullable=False),
        sa.Column("model_version", sa.String(40), nullable=True, comment="供应商返回的版本号"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, comment="成本，单位 1e-6 元"),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("is_fallback", sa.Boolean(), nullable=False, comment="是否走了备选供应商"),
        sa.Column("is_degraded", sa.Boolean(), nullable=False, comment="是否降档（私有化候选）"),
        sa.Column("ok", sa.Boolean(), nullable=False, comment="成功或最终失败"),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("input_digest", sa.String(64), nullable=True, comment="输入哈希，不留原文"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        comment=_TABLE_COMMENT,
    )
    # 单列索引：`index=True` 在 ORM 侧生成的就是这些名字，逐字对齐。
    for col in ("user_id", "consultation_id", "process_id", "agent", "task_type", "provider", "created_at"):
        op.create_index(f"ix_llm_call_{col}", "llm_call", [col])
    # 维度 + 时间的复合索引：看板按日/周/月查某维度，支撑"时间范围加维度"的聚合。
    op.create_index("ix_llm_call_task_created", "llm_call", ["task_type", "created_at"])
    op.create_index("ix_llm_call_agent_created", "llm_call", ["agent", "created_at"])


def downgrade() -> None:
    # 先 drop 表（索引随表一起删）。
    op.drop_table("llm_call")
