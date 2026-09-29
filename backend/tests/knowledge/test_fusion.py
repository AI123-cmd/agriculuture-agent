"""T2-49 验收：RRF 融合与去重（纯函数，不需要数据库）。

融合按名次而非分值，因此这些用例用最少的字段构造 `KnowledgeHit`，只关心 `knowledge_id`
与名次，不碰数据库。
"""

from app.knowledge.fusion import (
    RRF_K,
    KnowledgeHit,
    dedupe_by_id,
    reciprocal_rank_fusion,
)
from app.models.knowledge import KnowledgeItem


def _hit(kid: str, score: float = 0.0, channel: str = "vector") -> KnowledgeHit:
    """构造一个只带主键的命中。融合只看 id 与名次，其余字段无关。"""
    return KnowledgeHit(row=KnowledgeItem(knowledge_id=kid), score=score, channel=channel)


def _ids(hits: list[KnowledgeHit]) -> list[str]:
    return [h.row.knowledge_id for h in hits]


def test_rrf_identical_lists_preserve_order() -> None:
    """两路完全相同时保持原顺序：每条的贡献相同，名次不会被打乱。"""
    a = [_hit("K-1"), _hit("K-2"), _hit("K-3")]

    fused = reciprocal_rank_fusion(a, list(a))

    assert _ids(fused) == ["K-1", "K-2", "K-3"]


def test_rrf_single_channel_item_scores_nonzero() -> None:
    """只在单路出现的条目仍得到非零分，不会被融合丢弃。"""
    fused = reciprocal_rank_fusion([_hit("K-1")], [_hit("K-2")])

    assert set(_ids(fused)) == {"K-1", "K-2"}
    assert all(h.score > 0 for h in fused)


def test_rrf_first_rank_score_is_reciprocal() -> None:
    """k=60 时，单路首名的分值恰为 1/(60+1)。"""
    fused = reciprocal_rank_fusion([_hit("K-1")], [])

    assert fused[0].score == 1.0 / (RRF_K + 1)


def test_rrf_both_channel_item_outranks_single_channel() -> None:
    """两路同时命中的条目排名高于仅单路命中的条目——混合检索的核心效果。"""
    a = [_hit("K-both"), _hit("K-only-a")]
    b = [_hit("K-both"), _hit("K-only-b")]

    fused = reciprocal_rank_fusion(a, b)

    # K-both 在两条通路各得 1/61，其余各只得 1/61 一次。
    assert _ids(fused)[0] == "K-both"
    assert fused[0].score > fused[1].score


def test_rrf_item_in_both_accumulates_scores() -> None:
    """同一条目在两路各出现一次时分值为两次倒数排名之和。"""
    fused = reciprocal_rank_fusion([_hit("K-1")], [_hit("K-1")])

    assert len(fused) == 1
    assert fused[0].score == 2.0 / (RRF_K + 1)


def test_rrf_keeps_first_seen_hit_object() -> None:
    """同一条目在两路都出现时保留首次出现的命中对象（`setdefault` 语义）。"""
    first = _hit("K-1", score=0.9, channel="vector")
    second = _hit("K-1", score=0.1, channel="keyword")

    fused = reciprocal_rank_fusion([first], [second])

    assert fused[0].channel == "vector"


def test_rrf_ties_broken_by_id_for_determinism() -> None:
    """同分时按 id 升序，保证融合结果确定、可重放。"""
    fused = reciprocal_rank_fusion([_hit("K-b")], [_hit("K-a")])

    assert fused[0].score == fused[1].score
    assert _ids(fused) == ["K-a", "K-b"]


def test_rrf_empty_inputs() -> None:
    """两路都为空时返回空列表，不报错。"""
    assert reciprocal_rank_fusion([], []) == []


def test_dedupe_by_id_keeps_first_and_order() -> None:
    """保序去重保留首次出现者。"""
    hits = [_hit("K-2"), _hit("K-1"), _hit("K-2"), _hit("K-1"), _hit("K-3")]

    assert _ids(dedupe_by_id(hits)) == ["K-2", "K-1", "K-3"]


def test_dedupe_by_id_returns_new_list() -> None:
    """返回新列表，不改动入参。"""
    hits = [_hit("K-1")]
    out = dedupe_by_id(hits)

    assert out is not hits
