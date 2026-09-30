"""订阅额度与触达记录持久化模型（T3-01，技术方案 12.5）。

`subscription_quota` 是每户一行的一次性订阅额度账本，`reach_record` 是每一次触达尝试的
记录。二者是主动触达链路（11.6 / 11.7 / 11.8）的落点，本项只建表与列，额度扣减、失败分类
等逻辑分别在 T3-11 与 T3-14 实现。

三处刻意的选型：

1. `SubscriptionQuota.user_id` 直接作主键（一户一行），故不再需要额外唯一约束，也不再有
   `OwnedMixin` 的 `user_id` 单列索引（主键自带唯一索引）。子类显式声明 `user_id` 时，
   混入那一份被丢弃（见 `app/db/owned.py` 的说明）。
2. `remaining` 是冗余字段而非每次用 `granted_total - consumed_total` 现算：额度校验在推送上
   高频发生且必须原子扣减，冗余字段配合行级 `UPDATE ... WHERE remaining > 0` 才能无锁安全，
   一致性由 T3-11 的单条 SQL 保证（`user_id` 是主键，行级锁粒度即该户）。
3. `ReachRecord.prompt_item_id` 是 `nullable=False` 的外键，这是 11.8 顺序约束在数据层的落地：
   推送服务只能通过 `prompt_item_id` 取内容，不接受"直接传入文案"的调用方式。外键指向
   `prompt_item.id` 而非 `prompt_snapshot.id`，因为一次推送对应一条具体提示而非整份快照。

**外键 `ondelete` 保持默认（`RESTRICT`）**：快照、提示条目与触达记录都不允许被级联删除。
触达记录是送达率指标的原始数据，删除会污染统计口径。

`channel` / `status` / `fail_reason` 三列在本项只建列（`status` 与 `fail_reason` 的组合校验、
三者的枚举取值校验由 T3-14 补齐），取值语义见 `app/models/enums.py`。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.owned import OwnedMixin

_QUOTA_TABLE_COMMENT = "订阅额度账本：一户一行，remaining 冗余存储以支持无锁原子扣减"
_REACH_TABLE_COMMENT = "触达记录：每次触达尝试一行，含失败原因分类，是送达率指标的原始数据"


class SubscriptionQuota(Base, OwnedMixin):
    """一次性订阅额度账本。一户一行，`user_id` 即主键。

    `reject_streak` 是"长期无额度不反复索要"（11.7 第四条）的存储位，T3-11 据此决定授权
    入口是否降级为弱提示。
    """

    __tablename__ = "subscription_quota"
    __table_args__ = ({"comment": _QUOTA_TABLE_COMMENT},)

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="归属用户ID")
    granted_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="累计授予额度")
    consumed_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="累计消费额度")
    remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="剩余额度（冗余，原子扣减）")
    last_grant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_consume_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="连续拒绝次数，用于降级索要")


class ReachRecord(Base, OwnedMixin):
    """一次触达尝试的记录。

    `status="delivered"` 时 `fail_reason` 为 `NULL`，`status="failed"` 时 `fail_reason` 必非空；
    这条组合关系由 T3-14 的模型校验器实现，避免出现"失败了但没写原因"的记录。
    """

    __tablename__ = "reach_record"
    __table_args__ = (
        # 按提示条目反查触达记录：推送链路与复盘按条目取记录。
        Index("ix_reach_record_prompt_item_id", "prompt_item_id"),
        # 按用户 + 时间取触达记录：送达率与静默延迟统计的时间窗口查询。
        Index("ix_reach_record_user_attempted", "user_id", "attempted_at"),
        {"comment": _REACH_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prompt_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prompt_item.id", name="fk_reach_record_prompt_item"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, comment="subscribe_once/in_app")
    status: Mapped[str] = mapped_column(String(20), nullable=False, comment="delivered/failed")
    fail_reason: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="no_grant/quota_exhausted/api_error/quiet_hours"
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["ReachRecord", "SubscriptionQuota"]
