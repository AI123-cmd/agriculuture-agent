"""流程配置与流程红线的持久化模型（技术方案 12.1）。

两张表是配置的运行时事实来源：seeds（YAML）只在首次部署时作为初始数据源，之后
热加载读取的是库里的最新版本。落库的第二个用途是让任意一次历史问诊能追溯当时
生效的配置（`consultation.config_version` 回查 `by_version`），第三个用途是让后台
修订配置不必重新发版。

**本组表刻意不带 `user_id`**，这是全局约定「所有业务表带 `user_id`」的显式例外：
该约定的目的是防越权，而配置表对所有农户公开且只读，没有归属概念。这里写明是为
了避免后来者把它当作遗漏补上——补上会立刻产生「谁的配置」这个不存在的问题。

版本化用 `effective_from` / `effective_to` 两个时刻表达，而不是 `is_active` 布尔：
布尔回答不了「这条配置从什么时候开始生效」，而审计要按时间对齐。写入时「先封旧
再插新」在同一事务内完成，使区间首尾相接，任意时刻至多一条 `effective_to IS NULL`。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 配置状态三值。`deprecated` 是第三态而不是布尔里的「未生效」：被废弃的版本仍要能
# 被按版本号查回（历史问诊复现要用），只是不再参与「当前生效」的判断。
STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_DEPRECATED = "deprecated"

# 配置来源。`seed` 表示由同步任务从 YAML 写入，`manual` 表示后台人工修订。
SOURCE_SEED = "seed"
SOURCE_MANUAL = "manual"

_CONFIG_TABLE_COMMENT = "流程配置表：全局配置，刻意不带 user_id（对所有农户公开只读，无归属概念）"
_REDLINE_TABLE_COMMENT = "流程红线表：与配置同版本，独立成表以便按规则 id 反查"


class ProcessConfigRow(Base):
    """流程配置表。版本化：每次修改插入新行，不覆盖历史。"""

    __tablename__ = "process_config"
    __table_args__ = (
        # 显式命名唯一约束：与迁移 DDL 必须逐字一致，否则 autogenerate 会不断生成改名迁移。
        UniqueConstraint("process_id", "config_version", name="uq_process_config_version"),
        # 「当前生效版本」是最高频查询（`effective_to IS NULL`），本索引让它走索引扫描。
        Index("ix_process_config_active", "process_id", "effective_to"),
        {"comment": _CONFIG_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=STATUS_APPROVED, nullable=False, comment="draft/approved/deprecated"
    )
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="ProcessConfig 的规范化序列化")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="内容指纹，供热加载比对")
    source: Mapped[str] = mapped_column(String(20), default=SOURCE_SEED, nullable=False, comment="seed/manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="修订说明")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="为空表示当前生效"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProcessRedlineRow(Base):
    """流程红线表。与配置表同版本，独立成表以便按规则 id 反查。

    独立成表的价值集中在 `ix_process_redline_rule` 这个 GIN 索引上：闸门复盘需要回答
    「所有引用了 `R-INTERVAL` 的流程有哪些」，这在单表 JSON 里很难查，独立成表加索引后
    是一次简单查询。它同时是「声明了但没执行」反查的依据。
    """

    __tablename__ = "process_redline"
    __table_args__ = (
        UniqueConstraint("process_id", "config_version", "redline_id", name="uq_process_redline"),
        # JSONB 数组上的 GIN 索引：`rule_ids @> '["R-INTERVAL"]'` 走索引而非全表扫。
        Index("ix_process_redline_rule", "rule_ids", postgresql_using="gin"),
        {"comment": _REDLINE_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    redline_id: Mapped[str] = mapped_column(String(30), nullable=False, comment="如 TEA-01")
    desc: Mapped[str] = mapped_column(Text, nullable=False)
    rule_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, comment='["R-INTERVAL", "R-MAX-USE"]')
    severity: Mapped[str] = mapped_column(String(10), nullable=False, comment="block / warn")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


__all__ = [
    "SOURCE_MANUAL",
    "SOURCE_SEED",
    "STATUS_APPROVED",
    "STATUS_DEPRECATED",
    "STATUS_DRAFT",
    "ProcessConfigRow",
    "ProcessRedlineRow",
]
