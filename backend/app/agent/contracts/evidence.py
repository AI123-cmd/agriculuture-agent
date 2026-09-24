"""证据契约（技术方案 5.3.1、9.3）。

证据核查智能体的输入是 `EvidenceQuery`，输出是 `EvidenceSet`。两类失败在这里被显式表达：
「一条都没命中」不带原因地返回空集、以及命中集合里混入别的流程的条目。前者会让界面显示
一句无依据的判断，后者会把茶流程的药剂条目用在辣椒上——技术方案 9.3 明确"术语与判据不能
跨流程混用"，因此 `process_id` 是**硬过滤条件**而不是排序权重，并在契约层再自检一次。

`EvidenceItem` 定义在 `app/agent/state.py`（它是 `ConsultationState` 的元素类型），这里
只做再导出，避免同一形态存在两份定义。两者的差别只有一点：state 里的 `process_id` 是
`str`（TypedDict 要可 JSON 序列化），契约对象里的 `process_id` 是 `ProcessId`。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.state import EvidenceItem, ProcessId

# 知识条目种类的过滤集合。与 `EvidenceItem.kind` 的取值同源，但写成 `Literal` 是因为
# `kind_filter` 是检索请求的一部分，越界取值必须在构造请求时暴露，而不是到知识库层才
# 以空结果的形式静默出现。
EvidenceKindValue = Literal["pest", "disease", "pesticide", "cultivation", "threshold"]

# 地区回退链的实际落点（技术方案 9.3）。`none` 表示四级都没命中，与 `EvidenceSet.hit=False`
# 配合使用：级别记录了"退到哪一级"，命中与否记录了"退到那一级有没有结果"。
RegionLevelActual = Literal["county", "city", "province", "national", "none"]


class EvidenceQuery(BaseModel):
    """检索请求。

    `process_id` 为硬过滤条件而非软排序：若只做排序，茶流程的查询仍可能召回辣椒的药剂
    条目，后续重排序未必能纠正。硬过滤从候选集层面消除这个风险。

    `slot_snapshot` 是查询时刻的槽位摘要（`slot_summary` 的输出），用于让检索侧看到
    农户已经说了什么；它只是检索的上下文，不参与过滤。
    """

    model_config = ConfigDict(extra="forbid")

    process_id: ProcessId
    keywords: list[str]
    region_code: str | None = None
    slot_snapshot: dict[str, str] = {}
    kind_filter: list[EvidenceKindValue] = []
    top_k: int = 8


class EvidenceSet(BaseModel):
    """证据核查输出。命中为空时必须显式标注。"""

    model_config = ConfigDict(extra="forbid")

    query: EvidenceQuery
    items: list[EvidenceItem] = []
    hit: bool
    region_level_actual: RegionLevelActual
    reranked: bool = False
    unmatched_reason: str | None = None
    fallback_chain: list[str] = []

    @model_validator(mode="after")
    def _miss_requires_reason(self) -> "EvidenceSet":
        """技术方案 5.1 规定证据核查失败时"返回空集并标注未命中"，因此 `hit=False`
        是合法输出，但必须带 `unmatched_reason`。

        没有原因的空集在下游会被当成"知识库里确实没有相关条目"和"检索出了故障"两种
        情况的共同表现，判定智能体只能编造或含糊其辞。
        """
        if not self.hit and not self.unmatched_reason:
            raise ValueError("未命中时必须给出 unmatched_reason")
        return self

    @model_validator(mode="after")
    def _all_items_same_process(self) -> "EvidenceSet":
        """`process_id` 硬过滤的自检：即使检索实现有疏漏，跨流程证据也会在这里被拦住。

        这类"双保险"在安全相关路径上值得多写几行——检索实现会被反复改写（换向量库、
        换重排模型），而这条不变量必须在每次改写后依然成立。
        """
        bad = [i.knowledge_id for i in self.items if i.process_id != self.query.process_id.value]
        if bad:
            raise ValueError(f"证据跨流程: {bad}")
        return self


__all__ = [
    "EvidenceItem",
    "EvidenceKindValue",
    "EvidenceQuery",
    "EvidenceSet",
    "RegionLevelActual",
]
