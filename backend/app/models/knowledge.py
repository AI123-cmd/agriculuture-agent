"""知识库持久化模型（T2-47，技术方案 9.2）。

四层知识（disease / pest / pesticide / cultivation / threshold）共用 `knowledge_item`
一张表，以 `kind` 区分。这张表是整条安全链路的比对基准：闸门的 `R-WHITELIST`、
`R-REGISTERED-CROP`、`R-DOSAGE`、`R-INTERVAL` 都回查它，因此药剂类字段（剂量三元组、
登记作物、安全间隔期、季用次数、禁配组合）必须独立成列而非塞进 `content`——从正文做
模糊匹配会把"含某药名"的无关条目也召回，判定层无法区分。

`knowledge_id` 用具业务含义的字符串主键（形如 `K-1023`）而不是 UUID：它会被写进处方的
`facts_provenance`、出现在闸门拦截消息里、也被审计表引用，UUID 在这些位置不便人工核对。

`status` 四值语义由 `app/knowledge/status.py` 定义。`suspended`（抗药性暂停名单）建模为
状态而非删条目，是为了让历史问诊能回答"当时为什么没推荐这味药"，且恢复名单只需把
`status` 改回 `approved`，不必重新核一遍剂量与登记作物。

两张表都落在 `knowledge` schema 下。**索引一律显式命名**：带 schema 的表用 `index=True`
时 SQLAlchemy 生成的默认名会带冗余前缀，与迁移 DDL 对不上，autogenerate 随后会不断生成
无意义的改名迁移。向量索引 `ix_knowledge_embedding_hnsw` 同样在此声明——alembic 按"索引名
是否在元数据中"判断增删（`postgresql_using` / `postgresql_with` 不参与比对），若不在元数据
里声明，每次 autogenerate 都会误报"删除 HNSW 索引"。索引参数仍由迁移 DDL 决定。
"""

from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SCHEMA = "knowledge"
_ITEM_TABLE_COMMENT = "知识条目：四层知识共用一张表，以 kind 区分；闸门比对基准"
_REGION_TABLE_COMMENT = "知识条目地区适用范围：一条条目可覆盖多个行政区，支持分级回退匹配"


class KnowledgeItem(Base):
    """知识条目。四层知识共用一张表，以 `kind` 区分。"""

    __tablename__ = "knowledge_item"
    __table_args__ = (
        # 剂量区间必须有序；非药剂类条目的剂量字段可为空，故只在"两端都有值"时校验。
        CheckConstraint(
            "kind != 'pesticide' OR dosage_min IS NULL OR dosage_max IS NULL OR dosage_min <= dosage_max",
            name="ck_knowledge_dosage_range",
        ),
        # `suspended` 必须有理由：它是"临时停用"，没有理由的历史记录无法支撑追溯与恢复判断。
        CheckConstraint("status != 'suspended' OR status_reason IS NOT NULL", name="ck_knowledge_suspended_reason"),
        # 检索最常用的组合条件：按流程 + 状态 + 类型过滤候选集。
        Index("ix_knowledge_process_status_kind", "process_id", "status", "kind"),
        # 单列索引：带 schema 的表用 `index=True` 会生成冗余前缀名，故一律显式命名。
        Index("ix_knowledge_item_process_id", "process_id"),
        Index("ix_knowledge_item_kind", "kind"),
        Index("ix_knowledge_item_status", "status"),
        # 白名单/冲突规则按药剂名精确查，`R-WHITELIST` 与 `R-CONFLICT` 都依赖它。
        Index("ix_knowledge_item_pesticide_name", "pesticide_name"),
        # 地区过滤走 region_scope 的前缀条件（`region_scope LIKE '3301%'`）。
        Index("ix_knowledge_region_scope", "region_scope"),
        # HNSW 向量索引：名与迁移 DDL 逐字一致；参数（m / ef_construction）由迁移决定，
        # alembic 不比对 `postgresql_with`，故此处只需声明名字与列。
        Index(
            "ix_knowledge_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        {"schema": _SCHEMA, "comment": _ITEM_TABLE_COMMENT},
    )

    knowledge_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="disease/pest/pesticide/cultivation/threshold"
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="正文，用于检索与改写")

    # ---- 药剂类字段（kind=pesticide 时有值）----
    pesticide_name: Mapped[str | None] = mapped_column(
        String(80), nullable=True, comment="药剂名，白名单/冲突规则精确查"
    )
    registered_crops: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dosage_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    dosage_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    dosage_unit: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="克/亩、毫升/亩、倍液")
    safety_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_uses_per_season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forbidden_combinations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # ---- 适用范围与生效期 ----
    region_scope: Mapped[str | None] = mapped_column(String(12), nullable=True, comment="行政区编码前缀，NULL 表示全国")
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expire_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # ---- 状态与来源 ----
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="suspended 的理由必填")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True, comment="权威来源引用")
    # 内容指纹（T2-48）：仅覆盖影响向量与展示的字段，不含 status/regions。
    # 增量构建据此判断是否重算向量——只改 status 不应触发重新向量化。
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="内容指纹，供热加载比对")

    # ---- 检索 ----
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True, comment="pgvector 列，维度与 EMBEDDING_DIM 一致"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeRegion(Base):
    """地区适用范围：一条知识条目可覆盖多个行政区，支持分级回退匹配（T2-50）。"""

    __tablename__ = "knowledge_region"
    __table_args__ = (
        UniqueConstraint("knowledge_id", "region_code", name="uq_knowledge_region"),
        # 回退匹配按 (region_code, region_level) 逐级查表，复合索引让每级命中都走索引扫描。
        Index("ix_knowledge_region_code_level", "region_code", "region_level"),
        Index("ix_knowledge_region_knowledge_id", "knowledge_id"),
        Index("ix_knowledge_region_region_code", "region_code"),
        {"schema": _SCHEMA, "comment": _REGION_TABLE_COMMENT},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    region_code: Mapped[str] = mapped_column(String(12), nullable=False, comment="行政区编码")
    region_level: Mapped[str] = mapped_column(String(10), nullable=False, comment="county/city/province/national")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = ["KnowledgeItem", "KnowledgeRegion"]
