"""检索命中与倒数排名融合（T2-49）。

两路召回（向量 / 关键词）的分数尺度不同：余弦相似度落在 0~1，关键词得分依赖词频与查询
长度、取值范围不固定。直接把两者加权求和需要为每一路各调一个权重，而权重随查询变化、需
反复调参。RRF 只依赖**名次**，无需归一化也无需权重，代价是丢弃分差信息，因此融合后的分值
只用于排序，不对外暴露为"相关度"。

同一条目被两路同时召回时，RRF 自然把它往前推（两个名次贡献叠加）——语义与关键词同时命中
通常意味着该条目确实相关，这正是混合检索想要的效果。
"""

from dataclasses import dataclass

from app.models.knowledge import KnowledgeItem

# 两路召回的通路名。写入 `KnowledgeHit.channel` 便于测试与线上排查"这条是谁召回的"。
CHANNEL_VECTOR = "vector"
CHANNEL_KEYWORD = "keyword"

# RRF 的平滑常数。60 是文献常用默认值：它让排名靠后的条目分值衰减得更平缓，
# 使"两路都排在前列"的条目相比"单路第一"仍能被区分出来。
RRF_K = 60


@dataclass(slots=True)
class KnowledgeHit:
    """一次召回命中的条目。`score` 在融合后是 RRF 分值，融合前是单路原始分。"""

    row: KnowledgeItem
    score: float
    channel: str

    def with_score(self, score: float) -> "KnowledgeHit":
        """换一个分值、保留其余字段。融合时用它把单路分替换成 RRF 分。"""
        return KnowledgeHit(row=self.row, score=score, channel=self.channel)


def reciprocal_rank_fusion(
    a: list[KnowledgeHit],
    b: list[KnowledgeHit],
    k: int = RRF_K,
) -> list[KnowledgeHit]:
    """倒数排名融合：按名次而非分值合并两路结果。

    同一条目在 `a`、`b` 中若出现多次，只有首次出现的 `KnowledgeHit` 被保留（
    `setdefault`），但每一次出现都累加 `1/(k+rank)`；这正是"两路同时命中更靠前"的来源。
    """
    scores: dict[str, float] = {}
    kept: dict[str, KnowledgeHit] = {}
    for hits in (a, b):
        for rank, hit in enumerate(hits, start=1):
            kid = hit.row.knowledge_id
            scores[kid] = scores.get(kid, 0.0) + 1.0 / (k + rank)
            kept.setdefault(kid, hit)
    # 分值相同时按 id 升序，保证融合结果确定：否则同分条目的顺序随字典迭代次序漂移。
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [kept[kid].with_score(score) for kid, score in ordered]


def dedupe_by_id(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
    """按 `knowledge_id` 保序去重，保留首次出现者。

    单路召回的 SQL 不会返回重复行，但两条通路各自的候选合并前若不做去重，融合的
    排名基准会被同一 id 的多次出现污染。保留首次出现与 RRF 的 `setdefault` 语义一致。
    """
    seen: set[str] = set()
    out: list[KnowledgeHit] = []
    for hit in hits:
        kid = hit.row.knowledge_id
        if kid not in seen:
            seen.add(kid)
            out.append(hit)
    return out


__all__ = [
    "CHANNEL_KEYWORD",
    "CHANNEL_VECTOR",
    "RRF_K",
    "KnowledgeHit",
    "dedupe_by_id",
    "reciprocal_rank_fusion",
]
