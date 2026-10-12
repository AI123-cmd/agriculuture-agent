"""厂商报文模板模型（T5-02，技术方案 12.6）。

模板记录"某个型号的报文长什么样、每个字段对应什么标准指标、缩放与偏移是多少"。农户绑定时
若命中模板，第二步的"粘贴样例报文"与第三步的"逐字段点选"可一并省略，只需一次确认。

`vendor_template` 刻意**不继承 `OwnedMixin`**：模板是平台侧公共资产，不属于任何农户，农户只能
读不能写。这是全局约定"所有业务表带 `user_id`"的一个合理例外。`id` 用固定字符串（如
`tpl_generic_http`）而非随机 UUID，因为内置模板要能在多环境间稳定对应（T5-04 匹配、T5-06
排查都按 `id` 引用）。

`field_spec` 用 `JSONB`（方案 12.6 原文写裸 `JSON`）：它需要按键查询（按 `raw_field` 找映射）
与去重比较，`JSONB` 有结构化的索引与等值比较能力，与知识条目 `facts` 列的选型一致。

表结构由迁移 `0015_vendor_template` 显式建出，列与索引必须与迁移逐字对齐，否则
`tests/test_migrations.py::test_metadata_matches_orm` 会报差异。
"""

from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class VendorTemplate(Base, TimestampMixin):
    """厂商报文模板：覆盖主流型号，使第二步粘贴动作可省略。"""

    __tablename__ = "vendor_template"
    __table_args__ = (
        # 按厂商名筛取（运营维护模板时按厂商分组查看）。
        Index("ix_vendor_template_vendor", "vendor_name"),
        {"comment": "厂商报文模板：型号的字段预映射，使接入可跳过粘贴推断"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="固定字符串 id，如 tpl_generic_http")
    vendor_name: Mapped[str] = mapped_column(String(60), nullable=False, comment="厂商/模板名")
    model_pattern: Mapped[str | None] = mapped_column(String(80), nullable=True, comment="型号匹配正则")
    sample_payload: Mapped[str] = mapped_column(Text, nullable=False, comment="样例报文")
    field_spec: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="字段 → 指标预映射")


__all__ = ["VendorTemplate"]
