"""检索的元数据强过滤条件（T2-49，技术方案 9.3）。

过滤必须在**召回之前**在 SQL 层执行，而不是召回后再筛。向量与关键词两路召回各自只取
`top_k` 个候选，是有限预算；若先召回再过滤，跨流程与未核准条目会占满候选空间，真正可用
的条目被挤出，重排序也无力回天。

`build_filters` 返回的是**过滤描述**（普通字典）而非拼好的 SQL 语句。这样做有三个好处：
调用方（T2-50 的回退、T2-51 的重排）能复用同一份条件集；条件集可被单测直接断言（断言字典
内容比断言 SQL 字符串稳定）；SQL 的拼装集中在数据访问层一处。

`process_id` 是硬过滤而非排序权重，并且**只在自己流程与通用兜底之间展开**：通用条目
（`general`）无专属阈值、无药剂条目，允许混入任意流程；其余流程严格限定自身。展开为
`[query.process_id, ProcessId.GENERAL]` 而不是"不过滤"，跨流程条目仍在进入候选集之前被排除。
"""

from datetime import date

from sqlalchemy import ColumnElement, or_

from app.agent.contracts import EvidenceQuery
from app.agent.state import ProcessId
from app.knowledge.schema import NATIONAL_CODE
from app.knowledge.status import KnowledgeStatus
from app.models.knowledge import KnowledgeItem

# 可检索状态的唯一来源。写成常量而非散落各处，避免"顺手查全部状态"把未核准条目带进结果。
SEARCHABLE_STATUS = KnowledgeStatus.APPROVED.value


def build_filters(query: EvidenceQuery) -> dict:
    """把检索请求压成过滤描述字典。

    键的含义：
    - `process_id`：硬过滤的流程集合，`[自身, general]` 保序去重；
    - `status`：仅已核准；
    - `region_code`：本次（单级）检索的地区编码，`None` 表示只取全国兜底；
    - `effective_now`：是否按当天做生效期过滤；
    - `kind`：条目类型过滤集合，空集合表示不限。
    """
    processes: list[ProcessId] = []
    for pid in (query.process_id, ProcessId.GENERAL):
        if pid not in processes:
            processes.append(pid)
    return {
        "process_id": processes,
        "status": SEARCHABLE_STATUS,
        "region_code": query.region_code,
        "effective_now": True,
        "kind": list(query.kind_filter),
    }


def region_clause(region_code: str | None) -> ColumnElement[bool]:
    """地区过滤条件。

    条目 `region_scope` 存该条目适用的行政区编码；`NULL` 表示全国通用。查询发生在某一级
    （编码由调用方按级规范化后传入）时，只召回**恰好属于该级**的条目；退到全国一级时，
    连同 `region_scope` 为空的全国条目一起召回。逐级回退的判定循环在 T2-50，本函数只做
    单级匹配。
    """
    if region_code and region_code != NATIONAL_CODE:
        return KnowledgeItem.region_scope == region_code
    return or_(KnowledgeItem.region_scope == NATIONAL_CODE, KnowledgeItem.region_scope.is_(None))


def where_clauses(filters: dict, *, today: date | None = None) -> list[ColumnElement[bool]]:
    """把过滤描述翻译成一组 WHERE 表达式，供两路召回的 SQL 共用。"""
    clauses: list[ColumnElement[bool]] = [
        KnowledgeItem.status == filters["status"],
        KnowledgeItem.process_id.in_([pid.value for pid in filters["process_id"]]),
        region_clause(filters.get("region_code")),
    ]
    kinds = filters.get("kind") or []
    if kinds:
        clauses.append(KnowledgeItem.kind.in_(list(kinds)))
    if filters.get("effective_now"):
        day = today or date.today()
        # 生效期与失效期都可为空：为空表示不设该端边界，不参与过滤。
        clauses.append(or_(KnowledgeItem.effective_date.is_(None), KnowledgeItem.effective_date <= day))
        clauses.append(or_(KnowledgeItem.expire_date.is_(None), KnowledgeItem.expire_date >= day))
    return clauses


__all__ = ["SEARCHABLE_STATUS", "build_filters", "region_clause", "where_clauses"]
