"""农事台账条目模型（T3-15，技术方案 12.4 / 13.1 与产品说明书 4.5.2）。

台账把"AI 建议我做什么"（处方卡）沉淀为农户自己的、按时间排列的农事记录。本表只服务
`POST /api/v1/ai/prescription/{id}/ledger` 这一条写入路径，字段全部从处方卡**复制**而非
引用：药剂名、剂量、安全间隔期在入台账时做一次快照。理由是知识条目会随版本迭代
`deprecated`（12.7 允许引用已废弃条目），若只存 `knowledge_id`，未来回看时会因条目废弃
而显示不出当时的药剂名——台账的价值在于沉淀历史，必须自包含。

三处刻意的选型，均沿用本仓库既有约定：

1. JSON 列一律用 `JSONB`（与全库既有 JSON 列一致），而非裸 `JSON`。
2. `entry_date` 用 `Date` 而非 `DateTime`：无生育期流程（杂草防治、通用农业，见
   `app.broadcast.inputs.calendar.NO_CALENDAR_PROCESSES`）以 `entry_date` 为唯一时间轴，
   日粒度比较不涉及时区，用 `Date` 才不会被服务器时区影响的"当天"边界撕裂。
3. `source_type` 用 `String` 列而非 PostgreSQL 原生 ENUM，取值闭集由 `@validates` 调用
   `coerce_enum` 在 ORM 层强制（全局约定第四节"枚举封闭"），新增来源只改代码不动表。

幂等键是 `(user_id, source_type, source_id)`：农户会重复点击"存入台账"（尤其当他没看到
明确反馈时），若每次都新增一条，台账里会出现同一份处方的多条重复记录。`source_id` 对处方卡
就是 `card.id`。该唯一约束由迁移显式建出，同名 `uq_ledger_entry_source`。
"""

from datetime import date
from typing import Any

from sqlalchemy import Boolean, Date, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.db.owned import OwnedMixin
from app.models.enums import LedgerSourceType, coerce_enum

_LEDGER_TABLE_COMMENT = "农事台账：处方卡等沉淀的农事记录，按 entry_date 排列"


class LedgerEntry(Base, OwnedMixin):
    """一条农事台账记录。

    字段含义：`entry_date` 是农事发生日（无生育期流程的唯一时间轴）；`process_id` 是所属
    流程（落 `NO_CALENDAR_PROCESSES` 内时按纯时间线呈现）；`source_type` / `source_id`
    指向来源（当前只有 `prescription`）；`title` 取处方卡的 `diagnosis_summary`；
    `actions` / `pesticides` 是入台账时做的自包含快照；`followup_needed` 标记该处方是否
    登记了回访，供台账展示"待回访"状态。

    `actions` / `pesticides` / `facts_provenance` 用 Python 侧 `default=` 兜底而不写
    `server_default`：这是本仓库对 JSONB 列的既有约定，避免 `compare_server_default=True`
    持续生成"删除默认值"的假迁移。
    """

    __tablename__ = "ledger_entry"
    __table_args__ = (
        # 幂等键：同一来源（如一张处方卡）在同一农户下只允许一条台账记录。
        UniqueConstraint("user_id", "source_type", "source_id", name="uq_ledger_entry_source"),
        # 台账列表：按"用户 + 农事日期"倒序取数（无生育期流程的纯时间线亦然）。
        Index("ix_ledger_entry_user_date", "user_id", "entry_date"),
        {"comment": _LEDGER_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, comment="农事发生日")
    process_id: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="七条流程之一")
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="来源类型，如 prescription")
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="来源主键，处方的 card.id")
    title: Mapped[str | None] = mapped_column(String(120), nullable=True, comment="标题，取处方诊断摘要")
    actions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list, comment="处置动作快照")
    pesticides: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, comment="药剂条目快照（名/剂量）"
    )
    safety_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="安全间隔期（天）")
    facts_provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, comment="数值溯源表")
    followup_needed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否登记了回访")

    @validates("source_type")
    def _validate_source_type(self, key: str, value: Any) -> Any:
        return coerce_enum(LedgerSourceType, value)


__all__ = ["LedgerEntry"]
