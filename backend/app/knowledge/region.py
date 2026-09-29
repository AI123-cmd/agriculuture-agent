"""地区回退匹配与来源标注（T2-50，技术方案 9.3）。

给定农户所在行政区编码，构造一条从县级到全国的有序回退链，逐级检索，某一级召回非空即
停止，并把**实际命中的级别**写回 `EvidenceItem.region_scope` 与 `EvidenceSet.region_level_actual`。
它是产品说明书 4.1.2 功能点 1"缺失时按上一级行政区回退并在界面明示"在检索侧的落地。

回退不是"查不到就随便给一条"，而是"逐级放宽，并在结果里留下足迹"。判定循环放在本模块而不是
检索实现内部：T2-49 的 `search` 保持"一个查询一个地区"的单一语义（便于单测），"哪一级够用"
是一个独立的决策。本类实现 T2-18 的 `RegionMatcher` 协议（`build_chain`）。

**回退链里的编码一律是六位规范码**：`region_clause` 用等值比较命中 `region_scope` 列，而该列
存六位码。市级编码写成 `330100` 而非 `3301`，否则等值比较在市级恒不命中，回退链会静默落空。
上级编码由前缀右补零推导，属纯函数，见 `app/knowledge/region_codes.py`。
"""

from dataclasses import dataclass
from typing import Protocol, cast

from app.agent.contracts import EvidenceItem, EvidenceQuery
from app.agent.contracts.evidence import RegionLevelActual
from app.knowledge.region_codes import LEVELS
from app.knowledge.region_codes import build_chain as _build_chain

# 回退命中市级及以上时才生成展示标注；`city/province/national` 对应的展示名后缀由调用方按
# 行政区字典给出（如"杭州市"），本模块只负责给出级别事实与是否标注。
_NOTE_SUPPRESSED = frozenset({"county", "none"})


class KnowledgeSearcher(Protocol):
    """单级地区的知识检索接口。T2-49 的 `KnowledgeRepository.search` 满足它。

    刻意只声明单参 `search(query)`：回退判定在本模块完成，检索实现不做多级回退。
    """

    async def search(self, query: EvidenceQuery) -> list[EvidenceItem]: ...


@dataclass(slots=True)
class RegionSearchResult:
    """一次带回退的检索结果。

    `fallback_chain` 保留全量尝试路径（如 `["county:330102", "city:330100", ...]`）而非只留
    终点：界面在命中县级时也能展示"曾尝试的级别"，线上排查则最想知道"哪一级开始命中"。
    """

    items: list[EvidenceItem]
    region_level_actual: RegionLevelActual
    fallback_chain: list[str]

    def display_note(self, region_name: str) -> str | None:
        """生成界面的地区标注语。县级命中或完全未命中时不需要标注。

        只有回退到市级及以上时才标注：县级命中说明农户所在地有专属条目，界面无需额外说明；
        若这种情况也标注"当前按 XX 县提示"，会给农户造成"数据不全"的错觉。
        """
        if self.region_level_actual in _NOTE_SUPPRESSED:
            return None
        return f"当前按{region_name}通用提示"


class RegionMatcher:
    """地区回退匹配。构造回退链并逐级检索，保留实际命中级。

    `repository` 可省略：`build_chain` 是纯函数，仅构造回退链时无需检索实现。只有在调用
    `search_with_fallback` 时才必须提供。
    """

    def __init__(self, repository: KnowledgeSearcher | None = None) -> None:
        self._repo = repository

    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        """返回 (行政级别, 编码) 的有序回退链，从县级到全国。

        级别取值：county / city / province / national。委托给纯函数 `region_codes.build_chain`，
        使回退链的构造可离线测试，也让本方法不自持可变状态。
        """
        return _build_chain(region_code)

    async def search_with_fallback(self, query: EvidenceQuery) -> RegionSearchResult:
        """逐级检索，某级非空即停止。返回证据、实际命中级与尝试链。"""
        if self._repo is None:
            raise RuntimeError("未提供检索实现，无法执行回退检索")
        chain = self.build_chain(query.region_code)
        attempted: list[str] = []
        for level, code in chain:
            attempted.append(f"{level}:{code}")
            scoped = query.model_copy(update={"region_code": code})
            items = await self._repo.search(scoped)
            if items:
                return RegionSearchResult(
                    items=self._stamp(items, level, code),
                    region_level_actual=cast(RegionLevelActual, level),
                    fallback_chain=attempted,
                )
        return RegionSearchResult(items=[], region_level_actual="none", fallback_chain=attempted)

    def _stamp(self, items: list[EvidenceItem], level: str, code: str) -> list[EvidenceItem]:
        """把实际命中的行政级别写回每条证据的 `region_scope`。

        写成 `"county:330102"` 这样的带级别前缀形式而不是只写编码：展示层要据此生成"当前按
        XX 市通用农时提示"，只给编码还需再查一次级别，且编码本身不携带级别信息。带级别前缀
        让标注信息自包含。逐字段照抄时其余字段原样保留（`model_copy` 只改 `region_scope`）。
        """
        scope = f"{level}:{code}"
        return [item.model_copy(update={"region_scope": scope}) for item in items]


__all__ = [
    "LEVELS",
    "KnowledgeSearcher",
    "RegionMatcher",
    "RegionSearchResult",
]
