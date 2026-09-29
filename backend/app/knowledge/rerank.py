"""混合检索第四步：重排序（T2-51，技术方案 9.3 / 4.4）。

对 RRF 融合后的候选做相关性精排，输出截断到 `top_k` 的最终证据序列。给出三条实现路径：

- `HeuristicReranker`：规则加权，零模型可用，**默认实现**；
- `CrossEncoderReranker`：cross-encoder 精排，逐条拼接查询与候选打分；
- `FallbackReranker`：主实现不可用时回落到轻量实现，保证重排失败不中断检索。

默认走规则实现：重排序处在每次证据核查都经过的高频路径上，cross-encoder 需要一次远程调用，
会把证据核查延迟抬高一个量级。先用规则实现跑通链路，再按评测结果决定是否启用交叉编码器。
规则打分的信号（标题命中、有效期、地区亲和）都是**规则可判定**的，符合"凡是能被规则判定的
不交给模型"的约定，也保证这条链路在模型全线不可用时仍可用。

重排序必须满足两条确定性要求：同一份候选与查询两次调用返回相同顺序（规则打分无随机性）；
输出顺序不依赖输入顺序（`sorted` 的 key 只依赖 `hit` 与 `query`，不依赖下标）。并列时用
`knowledge_id` 做最终 tie-break。
"""

import asyncio
from datetime import date
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.knowledge.fusion import KnowledgeHit
from app.models.knowledge import KnowledgeItem
from app.observability import log, metrics

# 传给重排序的候选上限（来自方案 9.3 的 `fused[:30]`）。控制 cross-encoder 的打分条数：
# 两路各 20 条、去重后通常 25~35 条，30 能在覆盖融合前排的同时不溢出。
RERANK_CANDIDATE_LIMIT = 30

# 降级指标名。重排失败必须能被线上发现，故降级时上报。
FALLBACK_METRIC = "rerank_fallback_total"

# 规则打分的权重。语义明确、不设成可配置项，避免"调参调出无法复现的排序"：
# `hit.score` 是 RRF 融合分（主体权重），标题命中 0.30（标题比正文更代表主题），
# 有效期 0.10（生效期内优先），地区亲和 0.05（同前缀加分）。
_TITLE_WEIGHT = 0.30
_EFFECTIVE_WEIGHT = 0.10
_REGION_WEIGHT = 0.05


class Reranker(Protocol):
    """重排序接口。候选为 RRF 融合后的有序列表。"""

    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]: ...


def _truncate(hits: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
    """截断到 `top_k`，并重排名次前缀（从 1 开始）。

    重排名次而不是保留原始 RRF 名次，因为对外暴露的证据顺序决定下游取样，重排后的名次
    才是最终语义。输入少于 `top_k` 时返回全部且名次连续。
    """
    return [hit.with_rank(i) for i, hit in enumerate(hits[:top_k], start=1)]


def _title_overlap(title: str, query: str) -> float:
    """标题对查询的覆盖度：查询里有多少个字符出现在标题中（0~1）。

    逐字判断而非分词：查询改写的措辞与标题未必共用分词边界，逐字更稳。空查询返回 0，
    避免除零。
    """
    if not query:
        return 0.0
    hits = sum(1 for ch in set(query) if ch in title)
    return hits / len(set(query))


def _is_effective_now(row: KnowledgeItem) -> float:
    """生效期内的条目得 1，否则 0。生效期为空视为"不设边界"，也算有效。"""
    today = date.today()
    if row.effective_date is not None and row.effective_date > today:
        return 0.0
    if row.expire_date is not None and row.expire_date < today:
        return 0.0
    return 1.0


def _region_affinity(region_scope: str | None) -> float:
    """地区亲和：条目带地区限定时得 1，全国通用得 0。

    地区特异条目对具体地区查询的价值高于全国通用条目，故轻微加权。
    """
    return 1.0 if region_scope else 0.0


class HeuristicReranker:
    """轻量精排：规则加权，零模型可用。默认实现。"""

    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        # 按 (-score, knowledge_id) 排序：分值降序、并列时 id 升序。key 只依赖 hit 与 query，
        # 不依赖输入下标，因此输入顺序不影响输出（全局确定而非稳定相对顺序）。
        ranked = sorted(candidates, key=lambda hit: (-self._score(hit, query), hit.row.knowledge_id))
        return _truncate(ranked, top_k)

    def _score(self, hit: KnowledgeHit, query: str) -> float:
        """规则打分：RRF 分 + 标题命中 + 有效期 + 地区亲和。"""
        return (
            hit.score
            + _TITLE_WEIGHT * _title_overlap(hit.row.title, query)
            + _EFFECTIVE_WEIGHT * _is_effective_now(hit.row)
            + _REGION_WEIGHT * _region_affinity(hit.row.region_scope)
        )


def _reorder(candidates: list[KnowledgeHit], scores: list[float]) -> list[KnowledgeHit]:
    """把打分贴回候选并按分降序、id 升序重排。cross-encoder 的打分顺序与候选一一对应。"""
    scored = list(zip(candidates, scores, strict=True))
    scored.sort(key=lambda pair: (-pair[1], pair[0].row.knowledge_id))
    return [hit.with_score(score) for hit, score in scored]


class CrossEncoderReranker:
    """cross-encoder 精排：把查询与候选拼接后逐条打分。"""

    def __init__(self, url: str, api_key: str, timeout: float = 10.0) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout

    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        """批量打分后重排。空候选直接返回空列表，不发请求。"""
        if not candidates:
            return []
        pairs = [(query, f"{hit.row.title}\n{hit.row.content[:512]}") for hit in candidates]
        scores = await self._score(pairs)
        return _truncate(_reorder(candidates, scores), top_k)

    async def _score(self, pairs: list[tuple[str, str]]) -> list[float]:
        """调用 cross-encoder 服务批量打分。`temperature=0` 保证同输入同分值。"""
        payload = {"pairs": [{"query": q, "text": t} for q, t in pairs], "temperature": 0}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return [float(score) for score in data["scores"]]


class FallbackReranker:
    """主重排不可用时回落到轻量实现。"""

    def __init__(self, primary: Reranker, fallback: Reranker, *, timeout: float = 3.0) -> None:
        self._primary = primary
        self._fallback = fallback
        self._timeout = timeout

    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        """主实现超时或抛错时回落，并上报降级指标。

        降级路径把主实现换成轻量实现，而不是返回空集——空集会让判定误以为知识库没内容，
        从而输出"不确定"结论，而实际上候选是好的，只是精排服务挂了。
        """
        try:
            return await asyncio.wait_for(self._primary.rerank(query, candidates, top_k), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001  任何主实现失败都回落
            log.warning("rerank fallback", reason=str(exc), count=len(candidates))
            metrics.incr(FALLBACK_METRIC)
            return await self._fallback.rerank(query, candidates, top_k)


def build_reranker() -> Reranker:
    """按配置装配重排序实现（T2-51）。

    `RERANK_ENABLED=false`（默认）只构造 `HeuristicReranker`，**绝不实例化**
    `CrossEncoderReranker`：未启用时连远程端点都不该被构造。启用时才把交叉编码器包进
    `FallbackReranker`，让它的超时与失败回落到规则实现，保证重排失败不中断检索。

    装配收在这里而不是散在调用点：具体实现由配置决定，检索侧只依赖 `Reranker` 协议。
    """
    settings = get_settings()
    if not settings.RERANK_ENABLED:
        return HeuristicReranker()
    primary = CrossEncoderReranker(
        url=settings.RERANK_API_URL,
        api_key=settings.RERANK_API_KEY,
        timeout=settings.RERANK_TIMEOUT_SECONDS,
    )
    return FallbackReranker(primary, HeuristicReranker(), timeout=settings.RERANK_TIMEOUT_SECONDS)


__all__ = [
    "FALLBACK_METRIC",
    "RERANK_CANDIDATE_LIMIT",
    "CrossEncoderReranker",
    "FallbackReranker",
    "HeuristicReranker",
    "Reranker",
    "build_reranker",
]
