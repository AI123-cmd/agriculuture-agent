"""提示快照与提示条目持久化模型（T3-01，技术方案 12.5）。

`prompt_snapshot` 是一次生成的结果容器，`prompt_item` 是其内的单条五段式提示。两张表是
T3-02 至 T3-15 全部代码的落点：字段名一旦偏差，快照生成、条数控制、幂等锁、推送顺序约束、
执行率统计都会连锁返工，因此字段名与 12.5 逐字一致。

三处刻意的选型，均沿用本仓库既有约定：

1. `snapshot_date` 用 `Date` 而非 `DateTime`：幂等键的粒度是"自然日 + 农户 + 地块"（11.4），
   日粒度比较必须不涉及时区，用 `Date` 才不会被服务器时区影响的"当天"边界撕裂。
2. `generated_at` / `executed_at` 用 `DateTime(timezone=True)`：跨日弱化（11.5）与触达时段
   判断（11.9）要求精确到小时且不因时区漂移，裸 `DateTime` 会落成 `timestamp without time zone`
   （`app/db/base.py` 的 `TimestampMixin` 注释即为此）。
3. `facts_provenance` 用 `JSONB` 而非方案原文的 `JSON`：全库既有的 JSON 列
   （`gate_audit.prescription_snapshot`、`knowledge_item.registered_crops`、`event_outbox.payload`）
   一律为 `JSONB`，沿用 T5-02 的既有偏离（"方案原文 JSON，本仓库改 JSONB"）；闸门
   `R-PROVENANCE` / `R-NO-DATA` 要按字段路径扫描，`JSONB` 的等值比较也更可靠。

**可空性**：方案代码骨架省略了多数列的 `nullable`，但本仓库的落地口径是"语义必须的列一律
`NOT NULL`"（对照 `app/models/gate_audit.py` 与 T2-46 的落地）。故除 `farm_entry_id`（杂草流程
不绑定地块）与 `executed_at`（未勾选时为空）外全部 `NOT NULL`；`source_kind` 更由 12.5 正文
明示"不允许为空"。

**索引与方案骨架的一处刻意偏离**：12.5 骨架给 `farm_entry_id` / `snapshot_date` 标了
`index=True`，本项按任务文件的"索引清单"改为复合索引 `(user_id, snapshot_date)` 与
`(user_id, farm_entry_id, snapshot_date)`——这两个单列索引都是复合索引的前缀，单列版本完全
冗余（沿用 T1-13 `event_outbox` 对冗余单列索引的处理）。`user_id` 的单列索引由
`OwnedMixin` 自动带上，是全局约定"所有业务表带 user_id，索引必建"的强制项，保留。

六个状态列仍是 `String` 列而非 PostgreSQL 原生 ENUM（原生枚举新增取值要走 `ALTER TYPE`，
与"新增流程只改 YAML"的迭代方式冲突），取值闭集由 `@validates` 在 ORM 层强制：非法值在
赋值/构造时即抛 `ValueError`，而不是静默入库（全局约定第四节"枚举封闭"）。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.db.owned import OwnedMixin
from app.models.enums import EnvSource, Severity, SourceKind, coerce_enum

_SNAPSHOT_TABLE_COMMENT = "提示快照：一次生成的结果容器，幂等键=(user_id, farm_entry_id, snapshot_date, revision)"
_ITEM_TABLE_COMMENT = "提示条目：快照内的单条五段式提示，含被条数控制压制的项"


class PromptSnapshot(Base, OwnedMixin):
    """一次提示生成的结果容器。

    唯一约束 `(user_id, farm_entry_id, snapshot_date, revision)` 由迁移显式建立：
    没有它，并发写入同一日的两条快照会同时存在，幂等检查在提交后才生效就来不及了。
    `farm_entry_id` 可为空（杂草流程不绑定地块，见 12.2），而 PostgreSQL 唯一约束对 NULL
    不去重，故杂草流程另用 `farm_entry_id IS NULL` 的部分唯一索引兜底。
    """

    __tablename__ = "prompt_snapshot"
    __table_args__ = (
        UniqueConstraint("user_id", "farm_entry_id", "snapshot_date", "revision", name="uq_prompt_snapshot_daily"),
        # 幂等键与列表查询的主索引：按"用户 + 自然日"取当天快照。
        Index("ix_prompt_snapshot_user_date", "user_id", "snapshot_date"),
        # 带地块的幂等键：同日同地块只应有一份有效快照。
        Index("ix_prompt_snapshot_user_farm_date", "user_id", "farm_entry_id", "snapshot_date"),
        # 部分唯一索引：杂草流程（farm_entry_id IS NULL）的同日同版本唯一性。
        # 唯一约束对 NULL 不去重，只能靠这条部分索引补上。
        Index(
            "uq_prompt_snapshot_daily_no_farm",
            "user_id",
            "snapshot_date",
            "revision",
            unique=True,
            postgresql_where=text("farm_entry_id IS NULL"),
        ),
        {"comment": _SNAPSHOT_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    farm_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    env_source: Mapped[str] = mapped_column(String(20), nullable=False, comment="sensor/sensor_stale/weather/none")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="同日第几次更新")
    invalidated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="逻辑失效标记，不物理删除"
    )

    @validates("env_source")
    def _validate_env_source(self, key: str, value: Any) -> Any:
        return coerce_enum(EnvSource, value)


class PromptItem(Base, OwnedMixin):
    """快照内的一条提示条目。

    `callout / fact / impact / action` 四段加 `severity` 正好是产品说明书 4.3.3 功能点 2 的
    五段式（呼告 → 级别 → 事实 → 影响 → 行动），`severity` 展示时必须前置。
    `facts_provenance` 沿用与处方一致的 `{"字段路径": "来源标识"}` 结构（全局约定第四节），
    使闸门的 `R-PROVENANCE` 与 `R-NO-DATA` 可同一套逻辑扫描。

    `source_kind` 是三路输入（环境事实 / 农事历 / 问诊回访）的判据，不允许为空；
    `suppressed=True` 的条目仍要落库，产品复盘"哪些内容长期被压制"依赖它（11.3）。
    """

    __tablename__ = "prompt_item"
    __table_args__ = (
        # 按快照取条目：视图组装与展示列表的主查询。
        Index("ix_prompt_item_snapshot_id", "snapshot_id"),
        # 部分索引：只含未被压制的行，供执行率统计使用（T3-15）。索引大小与被统计量
        # 成正比而非与历史总量成正比，理由与 T1-13 的部分索引一致。
        Index(
            "ix_prompt_item_unsuppressed",
            "snapshot_id",
            postgresql_where=text("suppressed = false"),
        ),
        {"comment": _ITEM_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prompt_snapshot.id", name="fk_prompt_item_snapshot"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False, comment="env_risk/calendar/followup")
    severity: Mapped[str] = mapped_column(String(10), nullable=False, comment="urgent/notice/reminder")
    callout: Mapped[str] = mapped_column(String(30), nullable=False, comment="呼告（对象）")
    fact: Mapped[str] = mapped_column(Text, nullable=False, comment="事实")
    impact: Mapped[str] = mapped_column(Text, nullable=False, comment="影响")
    action: Mapped[str] = mapped_column(Text, nullable=False, comment="行动")
    facts_provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, comment="事实来源映射，形如 {字段路径: 来源标识}"
    )
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False)
    priority_score: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False, comment="排序键，三维拼接为稳定分数")
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="被条数控制截断")
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="执行勾选（北极星指标）")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("source_kind")
    def _validate_source_kind(self, key: str, value: Any) -> Any:
        return coerce_enum(SourceKind, value)

    @validates("severity")
    def _validate_severity(self, key: str, value: Any) -> Any:
        return coerce_enum(Severity, value)


__all__ = ["PromptItem", "PromptSnapshot"]
