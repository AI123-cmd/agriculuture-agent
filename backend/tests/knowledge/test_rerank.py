"""T2-51 验收：重排序（规则精排 / cross-encoder / 降级 / 截断 / 确定性）。

这些用例**不碰数据库**：重排序的输入是 RRF 融合后的 `KnowledgeHit` 列表，输出是截断并
重排名次的列表，全程是纯函数 + 一个可选远程调用。用最少的字段构造条目即可断言排序结论。

两条确定性要求各有一条用例：同一输入两次调用顺序相同、候选输入顺序不影响输出顺序。
cross-encoder 的远程调用用 `respx` 拦截，而规则实现有一条"零网络请求"的反向断言——
规则路径是默认路径，它绝不该悄悄走网络。
"""

import asyncio
from datetime import date, timedelta

import httpx
import pytest
import respx

from app.agent.contracts import EvidenceQuery
from app.agent.state import ProcessId
from app.core.config import get_settings
from app.knowledge.fusion import KnowledgeHit
from app.knowledge.rerank import (
    FALLBACK_METRIC,
    RERANK_CANDIDATE_LIMIT,
    CrossEncoderReranker,
    FallbackReranker,
    HeuristicReranker,
    build_reranker,
)
from app.knowledge.repository import RERANK_LIMIT, KnowledgeRepository
from app.models.knowledge import KnowledgeItem
from app.observability import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    """指标是进程内单例，用例之间必须清零，否则降级计数会累积。"""
    metrics.reset()
    yield
    metrics.reset()


def _hit(
    kid: str,
    *,
    score: float = 0.0,
    title: str = "",
    content: str = "",
    region_scope: str | None = None,
    effective_date: date | None = None,
    expire_date: date | None = None,
) -> KnowledgeHit:
    """构造一条只带重排序所需字段的命中。重排序只看这些字段，其余字段无关。"""
    return KnowledgeHit(
        row=KnowledgeItem(
            knowledge_id=kid,
            title=title,
            content=content,
            region_scope=region_scope,
            effective_date=effective_date,
            expire_date=expire_date,
        ),
        score=score,
        channel="vector",
    )


def _ids(hits: list[KnowledgeHit]) -> list[str]:
    return [h.row.knowledge_id for h in hits]


def _ranks(hits: list[KnowledgeHit]) -> list[int]:
    return [h.rank for h in hits]


class _BoomReranker:
    """主实现替身：调用即抛错，用于验证降级。"""

    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        raise RuntimeError("cross-encoder down")


class _SlowReranker:
    """主实现替身：超过降级超时仍未返回，触发 `asyncio.wait_for` 超时。"""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        import asyncio

        await asyncio.sleep(self._delay)
        return candidates[:top_k]


# ---- 候选上限与截断 ----


def test_rerank_candidate_limit_is_thirty() -> None:
    """传重排序的候选上限为 30（技术方案 9.3 的 `fused[:30]`）。"""
    assert RERANK_CANDIDATE_LIMIT == 30


async def test_truncate_returns_top_k_with_consecutive_ranks() -> None:
    """`top_k=3` 时只返回 3 条，且名次为 1、2、3。"""
    candidates = [_hit(f"K-{i}", score=1.0 - i * 0.1) for i in range(5)]

    out = await HeuristicReranker().rerank("查询", candidates, top_k=3)

    assert len(out) == 3
    assert _ranks(out) == [1, 2, 3]


async def test_truncate_shorter_than_top_k_keeps_all_with_consecutive_ranks() -> None:
    """输入少于 `top_k` 时返回全部，名次仍然连续（从 1 开始）。"""
    candidates = [_hit("K-1", score=0.9), _hit("K-2", score=0.5)]

    out = await HeuristicReranker().rerank("查询", candidates, top_k=8)

    assert len(out) == 2
    assert _ranks(out) == [1, 2]


async def test_empty_candidates_return_empty() -> None:
    """空候选返回空列表，不报错（两路召回都为空是合法输入）。"""
    assert await HeuristicReranker().rerank("查询", [], top_k=8) == []


# ---- 规则打分 ----


async def test_title_hit_gets_higher_score() -> None:
    """标题含查询词的候选排名高于同分但标题无关的候选。"""
    query = "茶小绿叶蝉"
    on_topic = _hit("K-hit", score=0.5, title="茶小绿叶蝉防治")
    off_topic = _hit("K-miss", score=0.5, title="无关条目")

    out = await HeuristicReranker().rerank(query, [off_topic, on_topic], top_k=2)

    assert _ids(out)[0] == "K-hit"


async def test_expired_scores_lower_than_effective() -> None:
    """有效期外的候选分数低于有效期内：同分情况下有效条目排在前面。"""
    today = date.today()
    effective = _hit("K-effective", score=0.5, effective_date=today - timedelta(days=1))
    expired = _hit("K-expired", score=0.5, expire_date=today - timedelta(days=1))

    out = await HeuristicReranker().rerank("查询", [expired, effective], top_k=2)

    assert _ids(out)[0] == "K-effective"


async def test_region_scoped_scores_higher_than_national() -> None:
    """带地区限定的候选比全国通用候选略占优（地区亲和加分）。"""
    scoped = _hit("K-scoped", score=0.5, region_scope="330100")
    national = _hit("K-national", score=0.5, region_scope=None)

    out = await HeuristicReranker().rerank("查询", [national, scoped], top_k=2)

    assert _ids(out)[0] == "K-scoped"


async def test_fused_score_dominates_secondary_signals() -> None:
    """融合分是主体权重：分差大于次级信号之和时，高分条目仍排前。"""
    high = _hit("K-high", score=1.0, title="无关")
    low = _hit("K-low", score=0.0, title="茶小绿叶蝉防治")

    out = await HeuristicReranker().rerank("茶小绿叶蝉", [low, high], top_k=2)

    assert _ids(out)[0] == "K-high"


# ---- 确定性与并列定序 ----


async def test_ties_broken_by_knowledge_id() -> None:
    """打分完全并列时用 `knowledge_id` 升序定序，保证全局确定。"""
    a = _hit("K-b", score=0.5)
    b = _hit("K-a", score=0.5)

    out = await HeuristicReranker().rerank("查询", [a, b], top_k=2)

    assert _ids(out) == ["K-a", "K-b"]


async def test_same_input_twice_returns_identical_order() -> None:
    """同一输入两次调用返回完全相同的顺序（重排序无随机性）。"""
    candidates = [_hit(f"K-{i}", score=0.5, title=f"标题{i}") for i in range(6)]
    reranker = HeuristicReranker()

    first = await reranker.rerank("查询", list(candidates), top_k=6)
    second = await reranker.rerank("查询", list(candidates), top_k=6)

    assert _ids(first) == _ids(second)
    assert _ranks(first) == _ranks(second)


async def test_output_independent_of_input_order() -> None:
    """候选输入顺序不影响输出顺序：排序是全局的，不是稳定的相对排序。"""
    candidates = [
        _hit("K-1", score=0.1, title="茶小绿叶蝉防治"),
        _hit("K-2", score=0.9),
        _hit("K-3", score=0.5),
    ]

    forward = await HeuristicReranker().rerank("茶小绿叶蝉", list(candidates), top_k=3)
    reversed_ = await HeuristicReranker().rerank("茶小绿叶蝉", list(reversed(candidates)), top_k=3)

    assert _ids(forward) == _ids(reversed_)


# ---- 零网络：规则实现不发起任何请求 ----


async def test_heuristic_reranker_issues_no_network_request(respx_mock: respx.Router) -> None:
    """`HeuristicReranker` 全流程零网络请求，在 mock 与模型不可用时均可用。"""
    candidates = [_hit(f"K-{i}", score=0.5, title="茶小绿叶蝉") for i in range(5)]

    await HeuristicReranker().rerank("茶小绿叶蝉", candidates, top_k=3)

    assert respx_mock.calls == []


def test_build_reranker_returns_heuristic_when_disabled(monkeypatch) -> None:
    """`RERANK_ENABLED=false` 时不实例化 `CrossEncoderReranker`，只给规则实现。"""
    monkeypatch.setenv("RERANK_ENABLED", "false")
    get_settings.cache_clear()
    try:
        reranker = build_reranker()
    finally:
        get_settings.cache_clear()

    assert isinstance(reranker, HeuristicReranker)


def test_build_reranker_wraps_cross_encoder_when_enabled(monkeypatch) -> None:
    """`RERANK_ENABLED=true` 时把交叉编码器包进降级壳，主实现不可用时仍可回落。"""
    monkeypatch.setenv("RERANK_ENABLED", "true")
    monkeypatch.setenv("RERANK_API_URL", "https://rerank.example.com/score")
    get_settings.cache_clear()
    try:
        reranker = build_reranker()
    finally:
        get_settings.cache_clear()

    assert isinstance(reranker, FallbackReranker)


# ---- cross-encoder ----


async def test_cross_encoder_reorders_by_remote_scores(respx_mock: respx.Router) -> None:
    """cross-encoder 按远程分数重排：第二条拿到最高分后升到首位。"""
    respx_mock.post("https://rerank.example.com/score").mock(
        return_value=httpx.Response(200, json={"scores": [0.1, 0.9]})
    )
    candidates = [_hit("K-1", score=0.5), _hit("K-2", score=0.5)]
    reranker = CrossEncoderReranker("https://rerank.example.com/score", "key")

    out = await reranker.rerank("查询", candidates, top_k=2)

    assert _ids(out) == ["K-2", "K-1"]
    assert _ranks(out) == [1, 2]


async def test_cross_encoder_empty_candidates_make_no_request(respx_mock: respx.Router) -> None:
    """空候选不发请求：没有候选就没有打分可言。"""
    reranker = CrossEncoderReranker("https://rerank.example.com/score", "key")

    assert await reranker.rerank("查询", [], top_k=8) == []
    assert respx_mock.calls == []


# ---- 降级 ----


async def test_fallback_on_primary_error_returns_fallback_result_and_bumps_metric() -> None:
    """主实现抛错时返回落实现结果，并上报 `rerank_fallback_total` 指标。"""
    candidates = [_hit("K-1", score=0.9), _hit("K-2", score=0.1)]
    reranker = FallbackReranker(_BoomReranker(), HeuristicReranker(), timeout=1.0)

    out = await reranker.rerank("查询", candidates, top_k=2)

    assert _ids(out) == ["K-1", "K-2"]
    assert metrics.counter_value(FALLBACK_METRIC) == 1


async def test_fallback_on_primary_timeout() -> None:
    """主实现超时时走降级：`asyncio.wait_for` 拦截长耗时，返回规则结果。"""
    candidates = [_hit("K-1", score=0.9), _hit("K-2", score=0.1)]
    reranker = FallbackReranker(_SlowReranker(delay=0.5), HeuristicReranker(), timeout=0.05)

    out = await reranker.rerank("查询", candidates, top_k=2)

    assert len(out) == 2
    assert metrics.counter_value(FALLBACK_METRIC) == 1


async def test_fallback_does_not_bump_metric_when_primary_succeeds() -> None:
    """主实现成功时不降级、不计指标：降级计数只反映真实故障。"""
    candidates = [_hit("K-1", score=0.9)]
    reranker = FallbackReranker(HeuristicReranker(), _BoomReranker(), timeout=1.0)

    await reranker.rerank("查询", candidates, top_k=1)

    assert metrics.counter_value(FALLBACK_METRIC) == 0


async def test_fallback_returns_fallback_not_empty_on_failure() -> None:
    """降级路径返回落实现结果而不是空集：空集会被误读成"知识库没内容"。"""
    candidates = [_hit("K-1", score=0.9), _hit("K-2", score=0.5)]
    reranker = FallbackReranker(_BoomReranker(), HeuristicReranker(), timeout=1.0)

    out = await reranker.rerank("查询", candidates, top_k=2)

    assert out != []
