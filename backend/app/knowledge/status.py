"""知识条目状态四值及其检索语义（技术方案 9.2）。

四值是闭集，且"是否可被检索"由 `SEARCHABLE_STATUSES` 单一常量表达，而不是散落在
各查询里写字面量 `== "approved"`。检索侧只放行已核准条目，其余三态在候选集层面被
排除——尤其是 `suspended`（抗药性暂停名单）必须是"条目还在、但检索强制排除"，而不是
删条目，否则历史问诊无法追溯"当时为什么没推荐这味药"（技术方案 9.2）。

`str, Enum` 而非 `StrEnum`：与 `app/agent/state.py` 等既有枚举保持同一写法，落库时
`status.value` 直接是字符串，调试输出又比 `StrEnum` 更显式。
"""

from enum import Enum


class KnowledgeStatus(str, Enum):
    """知识条目状态。落库为字符串，`status` 列长度为 20 足够。"""

    DRAFT = "draft"  # 待核准，不参与检索
    APPROVED = "approved"  # 已核准，唯一可被检索使用的状态
    SUSPENDED = "suspended"  # 临时停用，检索强制排除，如抗药性暂停名单
    DEPRECATED = "deprecated"  # 已废弃，保留用于历史处方追溯，不参与新检索


# 可被检索的状态集合。刻意做成 `frozenset` 而非 list：查表是 O(1)，且不可被就地修改。
SEARCHABLE_STATUSES: frozenset[KnowledgeStatus] = frozenset({KnowledgeStatus.APPROVED})


__all__ = ["SEARCHABLE_STATUSES", "KnowledgeStatus"]
