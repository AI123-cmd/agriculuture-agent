"""闸门的只读事实源协议（T2-34）。

闸门不依赖 `app.services`（L1 硬规则），因此不能直接持有知识库仓储类型；它只能声明
"我需要哪些方法"，由调用方（检索阶段）传入满足协议的实现。协议而非具体类还带来可测性：
规则单测可以喂三行假实现，不牵动数据库。

协议里刻意不放 `search`：闸门不做检索——检索属生成侧。闸门只核对"已被引用条目"的字段，
这是"独立仲裁"与"生成"在数据上的分界（技术方案 8.1）。
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeEntrySnapshot(Protocol):
    """知识条目只读快照，字段与知识条目表（T2-47）同名同义。"""

    knowledge_id: str
    name: str  # 药剂名
    status: str  # draft/approved/suspended/deprecated
    registered_crops: tuple[str, ...]
    region_scope: str | None
    # 剂量三元组：三者同进同出，缺一不可作数
    dosage_min: float | None
    dosage_max: float | None
    dosage_unit: str | None
    safety_interval_days: int | None
    max_uses_per_season: int | None
    forbidden_combinations: tuple[str, ...]


@runtime_checkable
class KnowledgeFacts(Protocol):
    """已核准知识条目的只读事实源。

    `get_entry` 返回 `None` 表示"该 knowledge_id 不在已核准库中"，与"条目存在但
    `status != approved`"是两件事：规则要分别给出提示语（模型越界 vs 知识库状态变更）。
    """

    def get_entry(self, knowledge_id: str) -> KnowledgeEntrySnapshot | None: ...

    def is_approved(self, knowledge_id: str) -> bool: ...

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]: ...


__all__ = ["KnowledgeEntrySnapshot", "KnowledgeFacts"]
