"""T2-50 验收：地区回退链的构造、逐级命中与来源标注。

回退链是纯函数（前缀推导 + 去重），逐级检索的判定循环在 `RegionMatcher.search_with_fallback`。
本文件用假检索实现记录调用序列，把"退到哪一级、调用了几次、标注成什么级别"变成可断言的事实。

重点断言一处易错点：`region_scope` 写成**六位规范码**（`city:330100` 而不是 `city:3301`），
因为检索过滤用等值比较命中六位的 `region_scope` 列，四位前缀在市级会静默落空。
"""

from datetime import date

from app.agent.contracts import EvidenceItem, EvidenceQuery
from app.agent.state import ProcessId
from app.knowledge.region import RegionMatcher, RegionSearchResult
from app.knowledge.region_codes import build_chain, dedupe_chain, parent_code, region_name


def _query(region_code: str | None = "330102", **overrides) -> EvidenceQuery:
    payload: dict = {
        "process_id": ProcessId.TEA,
        "keywords": ["茶小绿叶蝉"],
        "region_code": region_code,
        "top_k": 8,
    }
    payload.update(overrides)
    return EvidenceQuery(**payload)


def _item(knowledge_id: str = "K-1", **overrides) -> EvidenceItem:
    payload: dict = {
        "knowledge_id": knowledge_id,
        "title": "茶小绿叶蝉防治",
        "content": "若虫期用药效果最好。",
        "process_id": "tea",
        "region_scope": None,
        "effective_date": date(2024, 1, 1),
        "kind": "pest",
        "score": 0.9,
    }
    payload.update(overrides)
    return EvidenceItem(**payload)


class _RecordingSearcher:
    """假检索：按 `region_code` 决定是否命中，并记录每次收到的地区编码。"""

    def __init__(self, hits: dict[str, list[EvidenceItem]]) -> None:
        self._hits = hits
        self.calls: list[str | None] = []

    async def search(self, query: EvidenceQuery) -> list[EvidenceItem]:
        self.calls.append(query.region_code)
        return list(self._hits.get(query.region_code or "", []))


# ---- 回退链的构造（纯函数） ----


def test_build_chain_four_levels_for_county() -> None:
    """县级编码推出四级链：县、市、省、全国，编码一律六位规范码。"""
    assert build_chain("330102") == [
        ("county", "330102"),
        ("city", "330100"),
        ("province", "330000"),
        ("national", "000000"),
    ]


def test_build_chain_none_only_national() -> None:
    """档案未填地区时回退链只有全国一级。"""
    assert build_chain(None) == [("national", "000000")]


def test_build_chain_dedupes_short_code() -> None:
    """`330000` 在县、市、省三级都推出同一编码，去重后不出现重复的市级/省级。"""
    chain = build_chain("330000")

    codes = [code for _, code in chain]
    assert codes.count("330000") == 1
    assert ("city", "330000") not in chain
    assert ("province", "330000") in chain
    assert chain[-1] == ("national", "000000")


def test_parent_code_pads_to_six_digits() -> None:
    """上级编码右补零到六位，而不是截断成四位前缀。"""
    assert parent_code("330102", "city") == "330100"
    assert parent_code("330102", "province") == "330000"


def test_build_chain_is_pure() -> None:
    """纯函数：连续调用同输入返回相等且不共享可变对象。"""
    first = build_chain("330102")
    second = build_chain("330102")

    assert first == second
    assert first is not second
    first.append(("x", "y"))
    assert build_chain("330102") == second


def test_dedupe_chain_keeps_general_level_for_canonical_code() -> None:
    """去重保留规范编码对应的最泛化级别：`330100` 属于市级而非县级。"""
    assert dedupe_chain([("county", "330100"), ("city", "330100")]) == [("city", "330100")]


def test_region_name_lookup() -> None:
    """字典把编码翻译成展示名，未知编码回退为编码本身。"""
    assert region_name("330100") == "杭州市"
    assert region_name("999999") == "999999"


# ---- 逐级检索与命中标注 ----


async def test_county_hit_stops_before_city() -> None:
    """县级有结果时只检索一次，标注为 county。"""
    searcher = _RecordingSearcher({"330102": [_item()]})
    matcher = RegionMatcher(searcher)

    result = await matcher.search_with_fallback(_query("330102"))

    assert searcher.calls == ["330102"]
    assert result.region_level_actual == "county"
    assert result.fallback_chain == ["county:330102"]
    assert result.items[0].region_scope == "county:330102"


async def test_falls_back_to_city_then_stops() -> None:
    """县级无结果、市级有结果时只检索两次，标注为 city。"""
    searcher = _RecordingSearcher({"330100": [_item()]})
    matcher = RegionMatcher(searcher)

    result = await matcher.search_with_fallback(_query("330102"))

    assert searcher.calls == ["330102", "330100"]
    assert result.region_level_actual == "city"
    assert result.fallback_chain == ["county:330102", "city:330100"]
    assert result.items[0].region_scope == "city:330100"


async def test_fallback_chain_records_all_attempted_levels() -> None:
    """全部级别均无结果时，尝试链记录四级，级别为 none。"""
    searcher = _RecordingSearcher({})
    matcher = RegionMatcher(searcher)

    result = await matcher.search_with_fallback(_query("330102"))

    assert result.items == []
    assert result.region_level_actual == "none"
    assert result.fallback_chain == ["county:330102", "city:330100", "province:330000", "national:000000"]


async def test_none_region_only_queries_national() -> None:
    """无编码时只查全国一级，命中时标注为 national。"""
    searcher = _RecordingSearcher({"000000": [_item()]})
    matcher = RegionMatcher(searcher)

    result = await matcher.search_with_fallback(_query(None))

    assert searcher.calls == ["000000"]
    assert result.region_level_actual == "national"
    assert result.fallback_chain == ["national:000000"]


async def test_stamp_preserves_other_fields() -> None:
    """`_stamp` 只改 `region_scope`，其余字段逐字段照抄不变。"""
    original = _item(knowledge_id="K-9", score=0.42, kind="pesticide", dosage_min=8.0, dosage_unit="克/亩")
    matcher = RegionMatcher()

    stamped = matcher._stamp([original], "city", "330100")[0]  # noqa: SLF001

    assert stamped.region_scope == "city:330100"
    assert stamped.model_dump(exclude={"region_scope"}) == original.model_dump(exclude={"region_scope"})
    assert original.region_scope is None


# ---- 展示标注 ----


def test_display_note_none_on_county_hit() -> None:
    """县级命中不需要标注。"""
    result = RegionSearchResult(items=[_item()], region_level_actual="county", fallback_chain=["county:330102"])

    assert result.display_note("上城区") is None


def test_display_note_for_fallback_levels() -> None:
    """市级及以上命中时给出通用提示语。"""
    for level in ("city", "province", "national"):
        result = RegionSearchResult(items=[], region_level_actual=level, fallback_chain=[])
        assert result.display_note("杭州市") == "当前按杭州市通用提示"


def test_display_note_none_when_nothing_hit() -> None:
    """完全未命中时不标注（由未命中说明承担）。"""
    result = RegionSearchResult(items=[], region_level_actual="none", fallback_chain=[])

    assert result.display_note("杭州市") is None
