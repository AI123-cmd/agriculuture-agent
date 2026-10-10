"""放宽 prompt_item.rule_id 到 120（T3-08 对接 T3-04）

Revision ID: 0011_prompt_item_rule_id_width
Revises: 0010_consultation
Create Date: 2026-09-30

`0009_prompt_snapshot` 把 `prompt_item.rule_id` 定义为 `String(40)`，其取值假设只来自
环境风险/日历类规则（如 `env_risk.pest_risk`、`calendar:{knowledge_id}` 均在 40 以内）。
T3-04 引入了回访条目，其 `rule_id` 由 `app/broadcast/inputs/consult.py` 生成为
`f"{f.content_rule or 'followup.effect_check'}:{f.id}"`：`content_rule` 是 `String(60)`，
`f.id` 形如 `fu_` + 24 位十六进制（27 字符），拼接后现实取值约 58、最坏约 88 字符，
稳定越过 40 上限。T3-08 是首处把回访 rule_id 落库的代码，实测插入报
`StringDataRightTruncationError: value too long for type character varying(40)`。

按"新文档优先"的对接原则，此处放宽列宽而非截断回访 rule_id（截断会破坏同日重复点击
的 rule_id 差集合并，同一规则判重会失真）。120 覆盖 60 + 1 + 36 的组成上限并留余量。

`prompt_item` 由 `0009_prompt_snapshot` 建出，故 `downgrade` 只需还原为 40；下游无引用
本列宽的结构，无需级联改动。与 ORM 侧 `app/models/snapshot.py` 的 `String(120)` 逐字对齐，
否则 `test_metadata_matches_orm` 会以非空 diff 报出。
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_prompt_item_rule_id_width"
down_revision = "0010_consultation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "prompt_item",
        "rule_id",
        existing_type=sa.String(40),
        type_=sa.String(120),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "prompt_item",
        "rule_id",
        existing_type=sa.String(120),
        type_=sa.String(40),
        existing_nullable=False,
    )
