"""知识条目状态变更记录模型（T2-54，技术方案 9.2）。

每个 `approved` 条目都是闸门规则的比对基准，因此"谁在什么时候把哪条改成了什么状态、
理由是什么"必须可查。本表就是这条追溯链的落点：只追加，不修改，不删除。

`from_status` / `to_status` 与 `action` 分开存，是因为二者语义不同：前者是状态快照，
后者是触发的动作。同一个动作在不同起始状态下可能得到不同目标（如 `deprecate` 可从
`approved` 或 `suspended` 发生），只存动作无法还原迁移路径。

`reason` 与主表 `knowledge_item.status_reason` 冗余是刻意的：主表那列是当前状态的
说明（供检索侧排查），本表是完整时间线（供追溯全历史）；只留其一都会在某个查询场景
下缺失信息。

`consulted_ref` 用于"因某次问诊触发的暂停"这类场景，可空——多数变更来自通报名单而非
单次问诊。`operator_id` 是弱引用，刻意不建外键：变更记录要在后台账号注销后仍可追溯。

表落在 `knowledge` schema 下（与 `knowledge_item` / `knowledge_region` 同域）。**索引一律
显式命名**：带 schema 的表用 `index=True` 时 SQLAlchemy 生成的默认名会带冗余前缀，与迁移
DDL 对不上，autogenerate 随后会不断生成无意义的改名迁移。本表无 `updated_at`，也不提供
更新/删除接口——不可变性由"结构上没有更新列 + 仓储只有追加与查询方法"表达（T2-54 验收）。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SCHEMA = "knowledge"
_TABLE_COMMENT = "知识条目状态变更记录：只追加，不修改，不删除"


class KnowledgeReviewLog(Base):
    """知识条目状态变更记录。只追加，不修改，不删除。"""

    __tablename__ = "knowledge_review_log"
    __table_args__ = (
        # 追溯某条目的完整变更时间线：按 knowledge_id + created_at 取数。
        Index("ix_review_log_kid_created", "knowledge_id", "created_at"),
        # 单列索引：带 schema 的表用 `index=True` 会生成冗余前缀名，故一律显式命名。
        Index("ix_review_log_knowledge_id", "knowledge_id"),
        # 按操作人复盘：某后台账号近期做了哪些变更。
        Index("ix_review_log_operator_id", "operator_id"),
        Index("ix_review_log_created_at", "created_at"),
        {"schema": _SCHEMA, "comment": _TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="submit/approve/reject/suspend/resume/deprecate"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="后台账号 id，弱引用不建外键")
    consulted_ref: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="关联的问诊，若因某次问诊触发")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = ["KnowledgeReviewLog"]
