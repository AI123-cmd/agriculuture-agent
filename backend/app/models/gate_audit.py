"""闸门判定审计模型（T2-46，技术方案 8.5）。

每次 `GateEngine.evaluate` 的结论都落一行，支撑 8.5 的三条复盘用途：block 事件人工复核、
按流程统计 degrade 率、核对 `checked_rules` 是否覆盖已声明规则。因此 `violations` 与
`checked_rules` 存结构化 JSONB 而不是拼接字符串——三条用途都要按 `rule_id`、`severity`、
`field` 聚合。

`config_version` 与 `checked_rules` 必须同时存在：第三条用途是"核对 `checked_rules` 是否
覆盖该流程 `redlines` 声明的全部规则 id"，而"声明了哪些"由 `config_version` 对应的配置
决定，缺任一都无法核对。

`shadowed` 是必要新增：影子模式下 `decision` 落的是真实判定但对外返回 `passed=True`，
若不标记则事后无法区分"真实拦截"与"影子期记录"，14.3 的"闸门拦截率"也会被影子行污染。

`session_id` / `user_id` 是弱引用，刻意不建外键：审计数据要在用户注销后仍保留。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.owned import OwnedMixin

_TABLE_COMMENT = "闸门判定审计：每次 evaluate 写入一行，供 8.5 的三条复盘用途"


class GateAudit(Base, OwnedMixin):
    """闸门判定审计。每次 evaluate 写入一行，供 8.5 的三条复盘用途。"""

    __tablename__ = "gate_audit"
    __table_args__ = (
        # 用途一（block 事件复核）与用途二（degrade 率随时间的走势）：按 decision + 时间取数。
        Index("ix_gate_audit_decision_created", "decision", "created_at"),
        # 用途三（按流程核对规则覆盖）：按 process_id + decision 过滤，配置变更回归验证用。
        Index("ix_gate_audit_process_decision", "process_id", "decision"),
        {"comment": _TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False, index=True, comment="allow/degrade/