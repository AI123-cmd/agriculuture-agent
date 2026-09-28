"""知识条目复核工单模型（T2-46，技术方案 8.5 第一条复盘用途）。

每个 `block` 事件触发一条工单：命中硬红线既可能是"知识库缺条目"（该药剂本应被检索侧
排除），也可能是"模型越界"（处方数字不在知识库内）。两条路径的处置完全不同——前者补
知识、后者调提示词——因此必须在工单里一次性带齐定位信息，而不是让复核人员回查审计表。

`rule_ids` 与 `knowledge_ids` 存 JSONB 数组而非拼接字符串：前者按规则分组（哪条规则
频繁误伤），后者直接定位"是这几条知识条目的数据有问题"。

本表**不带 `user_id`**：工单是运营侧的内部待办，不对农户可见，也没有归属概念（与
`process_config` 同类）。`process_id` 与 `config_version` 是弱引用，刻意不建外键——
审计与工单要在配置修订、条目下线之后仍能复盘。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 工单状态三值。`closed` 是终态而不是布尔里的"已处理"：复核结论（补知识 / 调提示词）
# 需要被保留，否则同一规则反复触发时无从判断上次到底改了什么。
STATUS_PENDING = "pending"
STATUS_IN_REVIEW = "in_review"
STATUS_CLOSED = "closed"

_TABLE_COMMENT = "知识条目复核工单：每个 block 事件落一行，供 8.5 第一条复盘用途"


class GateReviewTicket(Base):
    """知识条目复核工单。每个 block 事件触发一条。"""

    __tablename__ = "gate_review_ticket"
    __table_args__ = (
        # 待办列表按状态 + 时间取数，`status` 作前缀可让"只看未处理的"走索引扫描。
        Index("ix_gate_review_ticket_status_created", "status", "created_at"),
        # 按流程核对复核量，判断某条流程是否持续触发。
        Index("ix_gate_review_ticket_process_id", "process_id"),
        {"comment": _TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    knowledge_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    decision: Mapped[str] = mapped_column(String(10), nullable=False, comment="allow/degrade/block")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_PENDING, comment="pending/in_review/closed"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


__all__ = ["STATUS_CLOSED", "STATUS_IN_REVIEW", "STATUS_PENDING", "GateReviewTicket"]
