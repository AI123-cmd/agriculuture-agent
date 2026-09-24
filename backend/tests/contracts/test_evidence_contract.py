"""T2-10：证据契约的地区回退与未命中标注。

两类反例是重点：`hit=False` 却不说明原因（下游无法区分"库里没有"与"检索坏了"）、以及
命中集合里混入别的流程的条目（技术方案 9.3 禁止术语与判据跨流程混用）。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.agent.contracts import EvidenceItem, EvidenceQuery, EvidenceSet, dump_contract, load_contract
from app.agent.state import ProcessId


def _query(**overrides) -> dict:
    base = {
        "process_id": ProcessId.TEA,
        "keywords": ["茶小绿叶蝉", "叶片卷曲"],
        "region_code": "330100",
        "slot_snapshot": {"tea_leaf_symptom": "叶片卷曲"},
        "kind_filter": ["pest"],
        "top_k": 8,
    }
    base.update(overrides)
    return base


def _item(**overrides) -> EvidenceItem:
    base = {
        "knowledge_id": "kb_tea_001",
        "title": "茶小绿叶蝉防治",
        "content": "若虫期用药效果最好。",
        "process_id": "tea",
        "region_scope": "浙江省杭州市",
        "effective_date": date(2025, 3, 1),
        "kind": "pest",
        "score": 0.87,
    }
    base.update(overrides)
    return EvidenceItem(**base)


def _hit_set(**overrides) -> dict:
    base = {
        "query": EvidenceQuery(**_query()),
        "items": [_item()],
        "hit": True,
        "region_level_actual": "county",
        "fallback_chain": ["county:330100"],
    }
    base.update(overrides)
    return base


def test_valid_hit_passes() -> None:
    result = EvidenceSet(**_hit_set())
    assert result.hit is True
    assert result.reranked is False
    assert result.unmatched_reason is None
    assert len(result.items) == 1


def test_miss_without_reason_raises() -> None:
    """技术方案 5.1：证据核查失败返回空集并标注未命中，但必须说明原因。"""
    with pytest.raises(ValidationError, match="unmatched_reason"):
        EvidenceSet(**_hit_set(items=[], hit=False, region_level_actual="none"))


def test_miss_with_reason_passes() -> None:
    result = EvidenceSet(
        **_hit_set(
            items=[],
            hit=False,
            region_level_actual="none",
            unmatched_reason="知识库中无茶小绿叶蝉在杭州市的县级条目",
        )
    )
    assert result.hit is False
    assert result.items == []


def test_cross_process_item_raises() -> None:
    """混入其他流程的条目必须在下游之前被拦住——茶流程的查询不得召回辣椒的药剂。"""
    with pytest.raises(ValidationError, match="证据跨流程"):
        EvidenceSet(**_hit_set(items=[_item(knowledge_id="kb_chili_009", process_id="chili")]))


def test_cross_process_check_reports_offending_ids() -> None:
    with pytest.raises(ValidationError, match="kb_chili_009"):
        EvidenceSet(
            **_hit_set(items=[_item(), _item(knowledge_id="kb_chili_009", process_id="chili")]),
        )


def test_national_fallback_passes() -> None:
    """地区回退链的终点是全国通用：四级都没命中县级条目也是合法结果。"""
    result = EvidenceSet(
        **_hit_set(
            items=[_item(region_scope=None)],
            region_level_actual="national",
            fallback_chain=["county:330100", "city:330000", "province:330000", "national"],
        )
    )
    assert result.region_level_actual == "national"
    assert len(result.fallback_chain) == 4


@pytest.mark.parametrize("level", ["county", "city", "province", "national", "none"])
def test_all_region_levels_accepted(level: str) -> None:
    result = EvidenceSet(
        **_hit_set(
            items=[],
            hit=False,
            region_level_actual=level,
            unmatched_reason="未命中",
        )
    )
    assert result.region_level_actual == level


def test_unknown_region_level_raises() -> None:
    with pytest.raises(ValidationError):
        EvidenceSet(**_hit_set(region_level_actual="town"))


def test_reranked_is_recorded() -> None:
    """是否经过重排序要记录下来：重排序会改变条目顺序，线上排查需要区分两种排序来源。"""
    assert EvidenceSet(**_hit_set(reranked=True)).reranked is True


def test_query_rejects_unknown_process_id() -> None:
    with pytest.raises(ValidationError):
        EvidenceQuery(**_query(process_id="corn"))


def test_query_rejects_unknown_kind_filter() -> None:
    """`kind_filter` 越界必须在构造请求时暴露，否则会以空结果的形式静默出现。"""
    with pytest.raises(ValidationError):
        EvidenceQuery(**_query(kind_filter=["pest", "fertilizer"]))


def test_query_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        EvidenceQuery(**_query(min_score=0.5))


def test_query_defaults() -> None:
    q = EvidenceQuery(process_id=ProcessId.RICE, keywords=["稻飞虱"])
    assert q.region_code is None
    assert q.slot_snapshot == {}
    assert q.kind_filter == []
    assert q.top_k == 8


def test_evidence_set_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        EvidenceSet(**_hit_set(debug=True))


def test_query_fields_match_spec() -> None:
    assert set(EvidenceQuery.model_fields) == {
        "process_id",
        "keywords",
        "region_code",
        "slot_snapshot",
        "kind_filter",
        "top_k",
    }


def test_evidence_set_fields_match_spec() -> None:
    assert set(EvidenceSet.model_fields) == {
        "query",
        "items",
        "hit",
        "region_level_actual",
        "reranked",
        "unmatched_reason",
        "fallback_chain",
    }


def test_evidence_item_reexported_from_state() -> None:
    """`EvidenceItem` 只在 `state.py` 定义一份，契约侧再导出，避免同一形态存在两份定义。"""
    from app.agent.state import EvidenceItem as StateEvidenceItem

    assert EvidenceItem is StateEvidenceItem


def test_json_schema_generates() -> None:
    schema = EvidenceSet.model_json_schema()
    assert "region_level_actual" in schema["properties"]


def test_roundtrip_through_state_payload() -> None:
    """`EvidenceSet` 进 state 前压成 dict，取出时校验回对象；`date` 必须已变成字符串。"""
    original = EvidenceSet(**_hit_set())
    payload = dump_contract(original)
    assert isinstance(payload["items"][0]["effective_date"], str)
    assert payload["query"]["process_id"] == "tea"
    assert load_contract(EvidenceSet, payload) == original
