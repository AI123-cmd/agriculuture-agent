"""T2-49 验收：混合检索的五步骨架（硬过滤 / 并行召回 / 融合 / 组装 / 降级）。

跑在真实 PostgreSQL 上：`embedding` 是 pgvector 列、`knowledge` 是独立 schema，SQLite 无法
承载。向量侧用可控的假 `Embedder`（对任意文本返回同一固定向量），把"最近邻是谁"变成可断言
的事实；这样既不依赖真实语义相似度，又能验证"两路同时命中排名更高"这类排序结论。

`process_id` 硬过滤是本文件的重点：茶流程的查询绝不召回辣椒或水稻的条目，且这条要在候选集
层面成立，而不是靠重排序纠正。

会话一律用 `async with` 开启：漏关的会话会留下空闲事务，阻塞下一个用例的 TRUNCATE，表现为
整轮挂死。
"""

import asyncio
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agent.contracts import EvidenceQuery
from app.agent.state import EvidenceKind, ProcessId
from app.knowledge.embedding import EMBEDDING_DIM, MockEmbedder
from app.knowledge.errors import KnowledgeUnavailableError
from app.knowledge.filters import build_filters, where_clauses
from app.knowledge.repository import RERANK_LIMIT, RECALL_TOP_K, KnowledgeRepository
from app.models import KnowledgeItem
from app.repositories.knowledge import KnowledgeRepository as QueryRepository

pytestmark = pytest.mark.usefixtures("requires_postgres")


def _vec(seed: str) -> list[float]:
    """由文本派生的确定性 1024 维单位向量。用于给条目/查询铺设可控的向量距离。"""
    return MockEmbedder()._unit_vector(seed)  # noqa: SLF001  测试需要直接取确定性向量


class _FixedEmbedder:
    """对任意文本返回同一固定向量的假实现，让向量召回的顺序完全可控。"""

    dimensions = EMBEDDING_DIM

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self._vector) for _ in texts]


def _item(**overrides) -> KnowledgeItem:
    """构造一条可检索条目。默认是茶流程、已核准、全国通用、未设有效期。"""
    payload: dict = {
        "knowledge_id": "K-1000",
        "process_id": ProcessId.TEA.value,
        "kind": EvidenceKind.PEST.value,
        "title": "茶小绿叶蝉防治",
        "content": "若虫期用药效果最好。",
        "registered_crops": [],
        "forbidden_combinations": [],
        "region_scope": None,
        "status": "approved",
        "embedding": _vec("default"),
    }
    payload.update(overrides)
    return KnowledgeItem(**payload)


async def _insert(session_factory, *items: KnowledgeItem) -> None:
    async with session_factory() as db:
        for item in items:
            db.add(item)
        await db.commit()


def _repo(session_factory, embedder=None) -> KnowledgeRepository:
    """构造检索入口。`KnowledgeRepository` 自持会话工厂，每次召回各开一条会话，无需外层托管。"""
    return KnowledgeRepository(session_factory, embedder or _FixedEmbedder(_vec("query")))


async def _search(session_factory, embedder=None, **overrides) -> list:
    """端到端跑一次检索。"""
    return await _repo(session_factory, embedder).search(_query(**overrides))


def _query(**overrides) -> EvidenceQuery:
    payload: dict = {
        "process_id": ProcessId.TEA,
        "keywords": ["茶小绿叶蝉"],
        "kind_filter": [],
        "top_k": 8,
    }
    payload.update(overrides)
    return EvidenceQuery(**payload)


def _sql(stmt) -> str:
    """把语句编译成带字面量的字符串，供断言包里检查过滤条件确实写进了 SQL。"""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


# ---- 第 1 步：元数据强过滤描述 ----


def test_build_filters_expands_to_self_plus_general() -> None:
    """`process_id` 硬过滤展开为 `[自身, general]`，而不是放宽为不过滤。"""
    filters = build_filters(_query(process_id=ProcessId.TEA))

    assert filters["process_id"] == [ProcessId.TEA, ProcessId.GENERAL]
    assert filters["status"] == "approved"
    assert filters["effective_now"] is True


def test_build_filters_does_not_duplicate_general() -> None:
    """查询本身就是 `general` 时，流程集合里不应出现两个 `general`。"""
    filters = build_filters(_query(process_id=ProcessId.GENERAL))

    assert filters["process_id"] == [ProcessId.GENERAL]


def test_both_recall_sql_carry_status_and_process_filter() -> None:
    """两路召回的 SQL 都带 `status='approved'` 与 `process_id IN (...)`。"""
    clauses = where_clauses(build_filters(_query(process_id=ProcessId.TEA)))
    repo = QueryRepository(session=None)  # type: ignore[arg-type]  仅取语句，不执行

    for sql in (
        _sql(repo.vector_recall_stmt([0.0] * EMBEDDING_DIM, clauses, RECALL_TOP_K)),
        _sql(repo.keyword_recall_stmt(["茶小绿叶蝉"], clauses, RECALL_TOP_K)),
    ):
        assert "status = 'approved'" in sql
        assert "'tea'" in sql and "'general'" in sql
        assert "'chili'" not in sql and "'rice'" not in sql


def test_vector_recall_sql_excludes_null_embedding() -> None:
    """向量召回语句带 `embedding IS NOT NULL`：空向量参与距离计算会得到无意义结果。"""
    clauses = where_clauses(build_filters(_query()))
    repo = QueryRepository(session=None)  # type: ignore[arg-type]

    assert "embedding IS NOT NULL" in _sql(repo.vector_recall_stmt([0.0] * EMBEDDING_DIM, clauses, RECALL_TOP_K))


# ---- 召回预算与并发 ----


def test_recall_top_k_and_rerank_limit_constants() -> None:
    """两路召回各自取 20，交给重排序的候选上限为 30（技术方案 9.3）。"""
    assert RECALL_TOP_K == 20
    assert RERANK_LIMIT == 30


async def test_search_calls_both_channels_with_recall_top_k(session_factory, monkeypatch) -> None:
    """`search` 以 `RECALL_TOP_K` 调用两路召回，两路互不干扰。"""
    seen: dict[str, int] = {}

    async def fake_vector(self, text, clauses, top_k):  # type: ignore[no-untyped-def]
        seen["vector"] = top_k
        return []

    async def fake_keyword(self, text, clauses, top_k):  # type: ignore[no-untyped-def]
        seen["keyword"] = top_k
        return []

    monkeypatch.setattr(KnowledgeRepository, "_vector_recall", fake_vector)
    monkeypatch.setattr(KnowledgeRepository, "_keyword_recall", fake_keyword)

    await _search(session_factory)

    assert seen == {"vector": 20, "keyword": 20}


async def test_two_recalls_run_concurrently(session_factory, monkeypatch) -> None:
    """两路召回用 `asyncio.gather` 并发：各阻塞 0.1 秒，总耗时低于 0.15 秒。"""

    async def slow_vector(self, text, clauses, top_k):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)
        return []

    async def slow_keyword(self, text, clauses, top_k):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)
        return []

    monkeypatch.setattr(KnowledgeRepository, "_vector_recall", slow_vector)
    monkeypatch.setattr(KnowledgeRepository, "_keyword_recall", slow_keyword)

    started = time.perf_counter()
    await _search(session_factory)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15, f"两路召回疑似串行执行，耗时 {elapsed:.3f}s"


# ---- 端到端：硬过滤 / 融合 / 组装 ----


async def test_search_excludes_other_processes(session_factory) -> None:
    """茶流程的查询结果不含辣椒与水稻条目：`process_id` 是硬过滤。"""
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(knowledge_id="K-tea", process_id="tea", embedding=vector),
        _item(knowledge_id="K-chili", process_id="chili", embedding=vector),
        _item(knowledge_id="K-rice", process_id="rice", embedding=vector),
    )

    results = await _search(session_factory, _FixedEmbedder(vector))

    ids = {item.knowledge_id for item in results}
    assert "K-tea" in ids
    assert not ({"K-chili", "K-rice"} & ids)


async def test_general_entries_mix_into_other_processes(session_factory) -> None:
    """`general` 兜底条目允许混入其他流程结果，因为它本身无专属阈值。"""
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(knowledge_id="K-tea", process_id="tea", embedding=vector),
        _item(knowledge_id="K-general", process_id="general", embedding=vector),
    )

    results = await _search(session_factory, _FixedEmbedder(vector))

    assert {item.knowledge_id for item in results} == {"K-tea", "K-general"}


async def test_suspended_and_draft_entries_excluded(session_factory) -> None:
    """`suspended` 与 `draft` 条目不出现在结果中：只有已核准条目可检索。"""
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(knowledge_id="K-ok", status="approved", embedding=vector),
        _item(knowledge_id="K-susp", status="suspended", status_reason="抗性暂停", embedding=vector),
        _item(knowledge_id="K-draft", status="draft", embedding=vector),
    )

    results = await _search(session_factory, _FixedEmbedder(vector))

    assert {item.knowledge_id for item in results} == {"K-ok"}


async def test_kind_filter_applied(session_factory) -> None:
    """`kind_filter` 生效：只召回指定种类，药剂条目被排除在外。"""
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(knowledge_id="K-pest", kind="pest", embedding=vector),
        _item(knowledge_id="K-pesticide", kind="pesticide", embedding=vector),
    )

    results = await _search(session_factory, _FixedEmbedder(vector), kind_filter=["pest"])

    assert {item.knowledge_id for item in results} == {"K-pest"}


async def test_expired_and_not_yet_effective_entries_excluded(session_factory) -> None:
    """有效期外的条目被排除：早于生效期的被挡在前面，晚于失效期的被挡在后面。"""
    vector = _vec("query")
    today = date.today()
    await _insert(
        session_factory,
        _item(knowledge_id="K-active", effective_date=today - timedelta(days=10), embedding=vector),
        _item(knowledge_id="K-expired", expire_date=today - timedelta(days=1), embedding=vector),
        _item(knowledge_id="K-future", effective_date=today + timedelta(days=1), embedding=vector),
    )

    results = await _search(session_factory, _FixedEmbedder(vector))

    assert {item.knowledge_id for item in results} == {"K-active"}


async def test_both_channel_hit_outranks_single_channel(session_factory) -> None:
    """两路同时命中的条目排名高于仅单路命中的条目。

    `K-both` 的标题含检索词（关键词路命中）且向量与查询完全一致（向量路第一）；`K-vector`
    只被向量路召回（标题无检索词）。RRF 让 `K-both` 的两个名次贡献叠加，排到最前。
    """
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(knowledge_id="K-both", title="茶小绿叶蝉防治", content="若虫期用药。", embedding=vector),
        _item(knowledge_id="K-vector", title="无关条目", content="与查询无关。", embedding=_vec("far")),
    )

    results = await _search(session_factory, _FixedEmbedder(vector), keywords=["茶小绿叶蝉"])

    assert results[0].knowledge_id == "K-both"


async def test_vector_recall_excludes_null_embedding(session_factory) -> None:
    """`_vector_recall` 排除 `embedding IS NULL` 的条目：空向量参与距离计算会得到无意义结果。"""
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(knowledge_id="K-hasvec", embedding=vector),
        _item(knowledge_id="K-nullvec", embedding=None),
    )
    clauses = where_clauses(build_filters(_query()))

    repo = _repo(session_factory, _FixedEmbedder(vector))
    hits = await repo._vector_recall("茶小绿叶蝉", clauses, RECALL_TOP_K)  # noqa: SLF001

    ids = {hit.row.knowledge_id for hit in hits}
    assert "K-hasvec" in ids
    assert "K-nullvec" not in ids


async def test_evidence_fields_copied_verbatim(session_factory) -> None:
    """证据逐字段照抄：剂量三元组、登记作物、安全间隔期原样带出，`region_scope` 空值填 national。"""
    vector = _vec("query")
    await _insert(
        session_factory,
        _item(
            knowledge_id="K-pest",
            kind="pesticide",
            dosage_min=Decimal("8.0000"),
            dosage_max=Decimal("12.0000"),
            dosage_unit="克/亩",
            safety_interval_days=14,
            max_uses_per_season=2,
            registered_crops=["茶"],
            forbidden_combinations=["波尔多液"],
            embedding=vector,
        ),
    )

    results = await _search(session_factory, _FixedEmbedder(vector))

    item = results[0]
    assert item.knowledge_id == "K-pest"
    assert float(item.dosage_min) == 8.0
    assert float(item.dosage_max) == 12.0
    assert item.dosage_unit == "克/亩"
    assert item.safety_interval_days == 14
    assert item.max_uses_per_season == 2
    assert item.registered_crops == ["茶"]
    assert item.forbidden_combinations == ["波尔多液"]
    assert item.region_scope == "national"


async def test_search_respects_top_k(session_factory) -> None:
    """`top_k` 限制返回条数：重排序阶段按 `query.top_k` 截断。"""
    vector = _vec("query")
    await _insert(session_factory, *[_item(knowledge_id=f"K-{i}", embedding=vector) for i in range(5)])

    results = await _search(session_factory, _FixedEmbedder(vector), top_k=2)

    assert len(results) == 2


async def test_keyword_only_hit_still_returned(session_factory) -> None:
    """向量路为空（无向量条目）时，关键词路命中仍能返回——任一路有结果即可继续。"""
    await _insert(
        session_factory,
        _item(knowledge_id="K-kw", title="茶小绿叶蝉", content="若虫期用药。", embedding=None),
    )

    results = await _search(session_factory, _FixedEmbedder(_vec("query")))

    assert {item.knowledge_id for item in results} == {"K-kw"}


async def test_search_wraps_db_error_as_unavailable(session_factory, monkeypatch) -> None:
    """检索内部异常统一转成 `KnowledgeUnavailableError`，供上层降级处置。"""

    async def boom(self, text, clauses, top_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("connection reset")

    monkeypatch.setattr(KnowledgeRepository, "_vector_recall", boom)

    with pytest.raises(KnowledgeUnavailableError):
        await _search(session_factory)


async def test_search_returns_empty_when_no_candidate(session_factory) -> None:
    """候选集为空时返回空列表，不抛异常——空集是合法的检索结果。"""
    await _insert(session_factory, _item(knowledge_id="K-other", process_id="chili"))

    results = await _search(session_factory, _FixedEmbedder(_vec("query")))

    assert results == []


async def test_embedding_column_holds_1024_dims(session_factory) -> None:
    """落库的向量维度与 `EMBEDDING_DIM` 一致，检索链路的前提成立。"""
    vector = _vec("query")
    await _insert(session_factory, _item(knowledge_id="K-dim", embedding=vector))

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-dim"))).scalar_one()

    assert len(row.embedding) == EMBEDDING_DIM
