"""条目数据文件的 Pydantic 模型（T2-48）。

条目数据文件（T2-52 / T2-53 的 jsonl）在载入阶段就要把结构性问题拦住：字段名拼错
（`extra="forbid"`）、`kind=pesticide` 缺剂量区间或安全间隔期、`suspended` 缺理由。
把校验放在这里而不是闸门运行时，是因为闸门的 `R-DOSAGE` / `R-INTERVAL` 以条目字段为
比对基准，条目本身缺字段时规则无从判定——早报错比线上才发现便宜得多。

`content_fingerprint` 只覆盖影响向量与展示的字段（标题、正文、kind、药剂名），
**不含 `status` 与 `regions`**：后者变化不改变向量，不应触发重新向量化。增量构建靠
它判断"是否需要重算"，若把 status 算进去，把条目从 draft 改为 approved 就得重跑一次
向量化，纯属浪费。
"""

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.state import EvidenceKind, ProcessId
from app.knowledge.status import KnowledgeStatus

# 全国兜底行政区编码与层级。回退链最后一级的语义基础（T2-48 挂载、T2-49 过滤、T2-50 回退）。
# 放在本模块是因为它被索引构建器、检索过滤与地区回退三处共同引用，且本模块只依赖
# pydantic 与核心枚举，不会引入数据库或配置加载的传递依赖。
NATIONAL_CODE = "000000"
NATIONAL_LEVEL: Literal["national"] = "national"


class KnowledgeRegionSpec(BaseModel):
    """条目的一个地区挂载点。`region_level` 决定它在回退链的哪一级被命中。"""

    model_config = ConfigDict(extra="forbid")

    region_code: str
    region_level: Literal["county", "city", "province", "national"]


class KnowledgeSeedItem(BaseModel):
    """条目数据文件中的一条记录。字段与 `knowledge_item` 表对应。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str
    process_id: ProcessId
    kind: EvidenceKind
    title: str
    content: str

    # ---- 药剂类字段（kind=pesticide 时必填，见校验器）----
    pesticide_name: str | None = None
    registered_crops: list[str] = []
    dosage_min: float | None = None
    dosage_max: float | None = None
    dosage_unit: str | None = None
    safety_interval_days: int | None = None
    max_uses_per_season: int | None = None
    forbidden_combinations: list[str] = []

    # ---- 适用范围与生效期 ----
    region_scope: str | None = None
    regions: list[KnowledgeRegionSpec] = []
    effective_date: date | None = None
    expire_date: date | None = None

    # ---- 状态与来源 ----
    status: KnowledgeStatus = KnowledgeStatus.DRAFT
    status_reason: str | None = None
    source_ref: str | None = None

    @model_validator(mode="after")
    def _pesticide_fields_required(self) -> "KnowledgeSeedItem":
        """`kind=pesticide` 时强制给出药剂名、完整剂量区间与安全间隔期。

        这三项正是 `R-WHITELIST` / `R-DOSAGE` / `R-INTERVAL` 的比对基准，缺任一项
        规则就无法判定，必须在条目载入期拒绝。
        """
        if self.kind is EvidenceKind.PESTICIDE:
            if not self.pesticide_name:
                raise ValueError("kind=pesticide 必须给出 pesticide_name")
            if self.dosage_min is None or self.dosage_max is None or not self.dosage_unit:
                raise ValueError("kind=pesticide 必须给出完整剂量区间与单位")
            if self.safety_interval_days is None:
                raise ValueError("kind=pesticide 必须给出 safety_interval_days")
        return self

    @model_validator(mode="after")
    def _suspended_requires_reason(self) -> "KnowledgeSeedItem":
        """`suspended` 必须给出理由，与表上 `ck_knowledge_suspended_reason` 一致。"""
        if self.status is KnowledgeStatus.SUSPENDED and not self.status_reason:
            raise ValueError("status=suspended 必须给出 status_reason")
        return self

    def fingerprint_payload(self) -> dict:
        """指纹覆盖的字段集。单独成方法便于测试断言"哪些字段影响指纹"。"""
        return {
            "title": self.title,
            "content": self.content,
            "kind": self.kind.value,
            "pesticide_name": self.pesticide_name,
        }

    def content_fingerprint(self) -> str:
        """内容指纹，用于增量构建时判断是否需要重算向量。"""
        payload = self.fingerprint_payload()
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def embedding_text(self) -> str:
        """向量化的输入文本。只含标题与正文，不拼药剂字段（见 T2-48 实现要点）。"""
        return f"{self.title}\n{self.content}"


__all__ = ["NATIONAL_CODE", "NATIONAL_LEVEL", "KnowledgeRegionSpec", "KnowledgeSeedItem"]
