"""问诊会话与处方卡持久化模型（T3-04 前置，技术方案 12.4）。

这两张表本应由 T1-04 的迁移建出，实际缺失。T3-04（问诊与回访信号采集）读取
`consultation.status == observing` 的会话，故按 12.4 的定义补齐。表结构由迁移显式建出，
本文件只做映射，列、索引、注释必须与迁移逐字对齐，否则 `test_metadata_matches_orm` 会报差异。

三处刻意的选型：

1. JSON 列一律用 `JSONB`（与 `gate_audit.prescription_snapshot`、`knowledge_item` 等
   全库既有 JSON 列一致），而非 12.4 原文的裸 `JSON`。
2. 方案骨架省略了多数列的 `nullable`，本仓库口径是"语义必须的列一律 `NOT NULL`"；
   只有 `farm_entry_id`（杂草流程不绑定地块）、`result` / `result_note` / `closed_at`
   （回访未回填时为空）等真正可空的列才放开。
3. `status` / `kind` 用 `String` 列而非 PostgreSQL 原生 ENUM，取值闭集由 `@validates`
   调用 `coerce_enum` 在 ORM 层强制（全局约定第四节"枚举封闭"），新增取值只改代码不动表。

`consultation.id` 与外键关系：`id` 即 session_id（由 T2-33 的图驱动接口生成）。
`prescription_card.consultation_id` 与 `followup_task.consultation_id` /
`prescription_card_id` 是技术方案 12.7 的两条高优先级外键，用显式命名与迁移逐字对齐，
`ondelete` 保持默认 `RESTRICT`——处方与回访是审计链路，不允许级联删除。

**T3-04 读取的展示字段**：技术方案 11.2 的 `collect_consult_signals` 直接读
`c.object_label` / `c.topic_summary` / `c.days_ago()` 与 `f.object_label` / `f.ask` /
`f.content_rule`，而 12.4 骨架没有这些列。按"逻辑以新文档为主"，这里就地补齐：
`object_label` 来自 T2-09 分诊结果的对象显示名（如"水稻·3亩"），`topic_summary` 是本次
问诊的主题摘要，二者在 T2-33 落库时写入；`ask` / `content_rule` 来自 T2-11 处方契约的
`FollowupTask`，由 T3-09 登记回访任务时写入。`days_ago()` 与 `needs_followup_check()`
是派生判定（读 `update_time`），不落库。
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base, TimestampMixin
from app.db.owned import OwnedMixin
from app.models.enums import ConsultStatus, FollowupKind, FollowupStatus, coerce_enum

_CONSULTATION_TABLE_COMMENT = "问诊会话：一次图驱动问诊一行，id 即 session_id"
_CARD_TABLE_COMMENT = "处方卡：问诊产出的处方快照与闸门结论，含回访计划"
_FOLLOWUP_TABLE_COMMENT = "回访任务：处方卡登记的回访时点与结果，首页提示的数据来源"


class Consultation(Base, TimestampMixin, OwnedMixin):
    """一次问诊会话。

    `status` 的五值中 `observing` 是 T3-04 的关注点：处于观察期且未到回访时点的问诊要转为
    首页跟进提示。`stage` / `rounds` / `triage_snapshot` / `slots_snapshot` 供问诊复盘与
    T2-33 的断点恢复使用。

    时间列走 `TimestampMixin`（`create_time` / `update_time`）而非 12.4 骨架的
    `created_at` / `updated_at`：与同为会话表的 `AIConversation` 保持一致，全库时间口径统一。
    """

    __tablename__ = "consultation"
    __table_args__ = (
        # 首页取"观察期会话"与历史列表按用户 + 创建时间取数。
        Index("ix_consultation_user_created", "user_id", "create_time"),
        # 按状态筛取（T3-04 的 list_observing）。
        Index("ix_consultation_user_status", "user_id", "status"),
        {"comment": _CONSULTATION_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="= session_id")
    farm_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="关联经营条目")
    process_id: Mapped[str] = mapped_column(String(20), nullable=False, comment="七条流程之一")
    process_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="流程是否锁定")
    title: Mapped[str] = mapped_column(String(60), nullable=False, comment="会话标题")
    object_label: Mapped[str] = mapped_column(String(60), nullable=False, comment="对象显示名，如 水稻·3亩")
    topic_summary: Mapped[str] = mapped_column(String(120), nullable=False, comment="本次问诊主题摘要")
    stage: Mapped[str] = mapped_column(String(20), nullable=False, comment="当前阶段")
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="追问轮次")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="observing/resolved/followup/escalated/blocked"
    )
    triage_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, comment="分诊结果快照")
    slots_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, comment="最终槽位快照")

    @validates("status")
    def _validate_status(self, key: str, value: Any) -> Any:
        return coerce_enum(ConsultStatus, value)

    def days_ago(self, today: date | None = None) -> int:
        """距上次交互（`update_time`）的整天数，负值归零。

        `update_time` 由 `TimestampMixin` 维护，每轮追问都会刷新，因此它就是
        "上次交互"的时点。列带时区，这里只取其日期部分与 `today` 相减——跨时区
        的边界误差在"第几天该回访"这一粒度上可忽略，且不引入额外时区依赖。
        """
        last = self.update_time or self.create_time
        last_date = last.date() if isinstance(last, datetime) else last
        return max(((today or date.today()) - last_date).days, 0)

    def needs_followup_check(self, today: date, observation_days: int) -> bool:
        """观察期问诊是否已到期该跟进。

        判定是两段：`status == observing` 且距上次交互天数已达该流程的观察期阈值
        （`observation_days` 由调用方从流程配置取，模型不自行读取配置）。"未到
        T3-09 登记的回访时点"这一段由 `list_observing` 在查询层排除已有回访任务的
        会话来保证——两处合起来才是不与到期回访来源重复的完整条件。
        """
        if self.status != ConsultStatus.OBSERVING.value:
            return False
        return self.days_ago(today) >= observation_days


class PrescriptionCard(Base, OwnedMixin):
    """处方卡：问诊产出的处方与闸门结论。

    `safety_interval_days` / `max_uses_per_season` 冗余存顶层（与 `Prescription` 契约一致），
    茶流程的回访时点计算（T3-09）直接读这两个字段，不必解析 `pesticide_items`。

    `followup_plan` / `next_pick_date` 是 T3-09 登记回访任务时的取值来源（12.4 的处方卡含
    "回访计划"一栏，但原骨架未给列）。按"逻辑以新文档为主"就地补齐：`followup_plan` 存
    T2-21 产出的 `FollowupPlan`（`tasks` 内含 `due_offset_days` / `kind` / `ask` /
    `content_rule` / `window_based`），`next_pick_date` 存茶流程的下一轮采摘计划日期。
    二者可空：无回访计划的处方（如转人工降级）不登记回访，`next_pick_date` 仅茶流程有值。

    **T3-15（处方卡入台账）依赖的三列**：`created_at` 是入台账时 `entry_date` 的取值来源
    （技术方案 12.4 已给此列，`server_default=now()`，与 `Consultation` 的时间口径一致）；
    `process_id` 与 `diagnosis_summary` 用于台账条目的流程归属与标题，12.4 骨架未给，按
    "逻辑以新文档为主"就地补列。三者都可空或缺省，使既有构造（测试与问诊落库链路只给
    `id` / `consultation_id` / `user_id`）不必逐字段补齐——`created_at` 由数据库默认值兜底。
    """

    __tablename__ = "prescription_card"
    __table_args__ = (
        # 按问诊取处方卡：一次问诊的处方详情与回访登记都要按 consultation_id 反查。
        Index("ix_prescription_card_consultation_id", "consultation_id"),
        {"comment": _CARD_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    consultation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("consultation.id", name="fk_prescription_card_consultation"),
        nullable=False,
        comment="所属问诊",
    )
    diagnosis: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, comment="诊断结果")
    actions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list, comment="处置动作列表")
    pesticide_items: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list, comment="药剂条目列表")
    safety_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="安全间隔期（天）")
    max_uses_per_season: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每季使用上限")
    followup_plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="回访计划，来自处方契约")
    next_pick_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="下一轮采摘计划日期（茶）")
    facts_provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, comment="数值溯源表")
    gate_result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, comment="闸门结论")
    gate_config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="闸门配置版本")
    requires_tech_station: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否需转农技站"
    )
    ledger_saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否已存入台账")
    # T3-15：入台账的取值来源。`created_at` 由数据库默认值兜底，既有构造无需显式给值。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    process_id: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="七条流程之一")
    diagnosis_summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="诊断摘要，来自处方契约")
    # T6-09：人工抽检结论（14.5）。三值 usable/needs_fix/unusable，未标注为空。
    # 落在卡片上而非只落 `eval_run`：看板按天聚合走 `eval_run`，业务侧"这张卡被标注过什么"
    # 需要按卡片反查，故两处一并写。取值由 `app/models/eval_run.py` 的常量给出。
    quality_label: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="usable/needs_fix/unusable，未标注为空"
    )
    quality_labeled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="标注时点"
    )


class FollowupTask(Base, OwnedMixin):
    """回访任务：处方卡登记的回访时点。

    `due_at` 用绝对时间戳存（契约层是相对的 `due_offset_days`）：扫描到期任务按时间范围查询
    （`due_at <= now`），相对天数无法直接查，转换在登记时一次完成（T3-09）。

    `result` / `result_note` / `closed_at` 在农户回填前为空，故放开可空；`status` 的
    `pending / done / skipped` 三值由 `@validates` 强制。
    """

    __tablename__ = "followup_task"
    __table_args__ = (
        # 到期扫描主索引：`status='pending' AND due_at <= now()`（T3-04 的 list_due / T3-09 的扫描）。
        Index("ix_followup_task_status_due", "status", "due_at"),
        # 按问诊与按处方卡反查。
        Index("ix_followup_task_consultation_id", "consultation_id"),
        Index("ix_followup_task_prescription_card_id", "prescription_card_id"),
        {"comment": _FOLLOWUP_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    consultation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("consultation.id", name="fk_followup_task_consultation"),
        nullable=False,
        comment="所属问诊",
    )
    prescription_card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("prescription_card.id", name="fk_followup_task_prescription_card"),
        nullable=False,
        comment="对应处方卡",
    )
    kind: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="effect_check/risk_check/preserve_scene/observe/compliance_check"
    )
    object_label: Mapped[str] = mapped_column(String(60), nullable=False, comment="对象显示名，如 水稻·3亩")
    ask: Mapped[str] = mapped_column(String(120), nullable=False, comment="回访询问语，来自处方契约")
    content_rule: Mapped[str | None] = mapped_column(
        String(60), nullable=True, comment="内容规则标识，如 weed.damage_evidence_checklist"
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="到期时点")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", comment="pending/done/skipped")
    result: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="improved/unchanged/worse")
    result_note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="回填说明")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="关闭时点")

    @validates("kind")
    def _validate_kind(self, key: str, value: Any) -> Any:
        return coerce_enum(FollowupKind, value)

    @validates("status")
    def _validate_status(self, key: str, value: Any) -> Any:
        return coerce_enum(FollowupStatus, value)


__all__ = ["Consultation", "FollowupTask", "PrescriptionCard"]
