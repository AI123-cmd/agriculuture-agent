"""T2-48 验收：知识库索引构建器（校验 / 幂等 / dry-run / 向量维度 / 地区展开 / mock 零请求）。

跑在真实 PostgreSQL 上：`embedding` 是 pgvector 列、`knowledge` 是独立 schema、
地区唯一约束依赖真实数据库，SQLite 均无法承载。表由迁移建出（`migrated_db`），
因此这里同时验证了"迁移与 ORM 一致"这一前提。

向量化一律用 `MockEmbedder`：它确定性、零网络，让检索链路的顺序断言可复现。
另有一条 `respx` 零请求断言，证明 mock 模式不会误连真实向量化服务。
"""

from datetime import date
import math

import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agent.state import EvidenceKind, ProcessId
from app.knowledge.embedding import EMBEDDING_DIM, MockEmbedder
from app.knowledge.indexer import BuildReport, IndexBuildError, KnowledgeIndexer
from app.knowledge.schema import KnowledgeRegionSpec, KnowledgeSeedItem
from app.knowledge.status import KnowledgeStatus
from app.models import KnowledgeItem, KnowledgeRegion
from app.services.knowledge_index_service import build_embedder

pytestmark = pytest.mark.usefixtures("requires_postgres")

_AREA = KnowledgeRegionSpec(region_code="330102", region_level="county")


def _item(**overrides) -> KnowledgeSeedItem:
    """构造一条合格的非药剂条目；用 overrides 覆盖单个字段。"""
    payload = {
        "knowledge_id": "K-1023",
        "process_id": ProcessId.RICE,
        "kind": EvidenceKind.PEST,
        "title": "稻飞虱识别与防治",
        "content": "稻飞虱聚集在稻丛基部吸食汁液，重发田块出现冒穿枯死。",
        "status": KnowledgeStatus.APPROVED,
    }
    payload.update(overrides)
    return KnowledgeSeedItem.model_validate(payload)


def _pesticide(**overrides) -> KnowledgeSeedItem:
    """构造一条合格药剂条目（`kind=pesticide` 的字段组完整）。"""
    payload = {
        "knowledge_id": "K-2001",
        "process_id": ProcessId.RICE,
        "kind": EvidenceKind.PESTICIDE,
        "title": "吡虫啉在水稻上的使用方法",
        "content": "吡虫啉用于防治稻飞虱，注意轮换用药以延缓抗性。",
        "pesticide_name": "吡虫啉",
        "registered_crops": ["水稻"],
        "dosage_min": 8.0,
        "dosage_max": 12.0,
        "dosage_unit": "克/亩",
        "safety_interval_days": 14,
        "status": KnowledgeStatus.APPROVED,
    }
    payload.update(overrides)
    return KnowledgeSeedItem.model_validate(payload)


async def _count(session_factory, model, **filters) -> int:
    async with session_factory() as db:
        stmt = select(func.count()).select_from(model)
        for column, value in filters.items():
            stmt = stmt.where(getattr(model, column) == value)
        return (await db.execute(stmt)).scalar_one()


async def _build(session_factory, items, *, dry_run=False, embedder=None) -> BuildReport:
    async with session_factory() as db:
        indexer = KnowledgeIndexer(db, embedder or MockEmbedder())
        report = await indexer.build(items, dry_run=dry_run, check_process=False)
        await db.commit()
    return report


def test_pesticide_without_dosage_rejected() -> None:
    """`kind=pesticide` 缺剂量区间时在载入阶段被拒：这是 R-DOSAGE 的比对基准。"""
    with pytest.raises(ValidationError):
        _pesticide(dosage_min=None, dosage_max=None)


def test_pesticide_without_unit_rejected() -> None:
    """缺剂量单位同样被拒：`8~12` 没有单位无法判定超量与否。"""
    with pytest.raises(ValidationError):
        _pesticide(dosage_unit=None)


def test_pesticide_without_safety_interval_rejected() -> None:
    """缺安全间隔期被拒：这是 R-INTERVAL 的比对基准。"""
    with pytest.raises(ValidationError):
        _pesticide(safety_interval_days=None)


def test_suspended_without_reason_rejected() -> None:
    """`status=suspended` 缺理由时被拒，与表上 CHECK 约束一致。"""
    with pytest.raises(ValidationError):
        _item(status=KnowledgeStatus.SUSPENDED)


def test_unknown_field_rejected() -> None:
    """`extra=forbid`：字段名拼错要在载入期暴露，而不是静默丢字段。"""
    with pytest.raises(ValidationError):
        _item(pestcide_name="错别字")


async def test_build_twice_second_run_all_skipped(session_factory) -> None:
    """同一份条目连续构建两次：第一次全部 upsert，第二次全部 skipped 且行数不变。"""
    items = [_item(), _pesticide()]

    first = await _build(session_factory, items)
    assert first.upserted == 2
    assert first.skipped == 0
    assert first.errors == []
    rows_after_first = await _count(session_factory, KnowledgeItem)

    second = await _build(session_factory, items)
    assert second.upserted == 0
    assert second.skipped == 2
    assert await _count(session_factory, KnowledgeItem) == rows_after_first


async def test_dry_run_writes_nothing(session_factory) -> None:
    """dry-run 报出"本应写入"的条数，但不落任何库（CI 与预发校验的用法）。"""
    report = await _build(session_factory, [_item(), _pesticide()], dry_run=True)

    assert report.upserted == 2
    assert await _count(session_factory, KnowledgeItem) == 0
    assert await _count(session_factory, KnowledgeRegion) == 0


async def test_undeclared_region_falls_back_to_national(session_factory) -> None:
    """未声明任何地区的条目自动挂到全国兜底 `000000`，保证回退链末级可召回。"""
    await _build(session_factory, [_item(regions=[], region_scope=None)])

    regions = await _count(session_factory, KnowledgeRegion, knowledge_id="K-1023")
    assert regions == 1
    async with session_factory() as db:
        row = (
            await db.execute(select(KnowledgeRegion).where(KnowledgeRegion.knowledge_id == "K-1023"))
        ).scalar_one()
    assert row.region_code == "000000"
    assert row.region_level == "national"


async def test_declared_regions_expand_row_per_region(session_factory) -> None:
    """声明两个地区则地区表落两行，且条目的 `region_scope` 前缀照常落主表。"""
    await _build(
        session_factory,
        [
            _item(
                region_scope="3301",
                regions=[
                    KnowledgeRegionSpec(region_code="330102", region_level="county"),
                    KnowledgeRegionSpec(region_code="330100", region_level="city"),
                ],
            )
        ],
    )

    assert await _count(session_factory, KnowledgeRegion, knowledge_id="K-1023") == 2


async def test_approved_count_matches_input(session_factory) -> None:
    """构建后 approved 条目数与输入一致；draft 条目照常入库但不计入 approved。"""
    await _build(
        session_factory,
        [
            _item(knowledge_id="K-1", status=KnowledgeStatus.APPROVED),
            _item(knowledge_id="K-2", status=KnowledgeStatus.APPROVED),
            _item(knowledge_id="K-3", status=KnowledgeStatus.DRAFT),
        ],
    )

    assert await _count(session_factory, KnowledgeItem, status="approved") == 2
    assert await _count(session_factory, KnowledgeItem, status="draft") == 1


async def test_rebuild_skips_when_only_status_or_regions_change(session_factory) -> None:
    """仅改 status / regions 不改指纹：第二次构建应全部 skipped，不重算向量。"""
    await _build(session_factory, [_item()])
    assert await _count(session_factory, KnowledgeItem) == 1

    report = await _build(session_factory, [_item(status=KnowledgeStatus.DRAFT, regions=[_AREA])])
    assert report.skipped == 1
    assert report.upserted == 0


async def test_content_change_triggers_rebuild(session_factory) -> None:
    """正文变化改变指纹：第二次构建 upserted=1、skipped=0，且地区挂载随之同步。"""
    await _build(session_factory, [_item()])

    report = await _build(
        session_factory, [_item(content="改写后的正文，包含更多症状描述。", regions=[_AREA])]
    )
    assert report.upserted == 1
    assert report.skipped == 0

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()
    assert row.content.startswith("改写后的正文")
    # 主表更新与地区表同步在同一事务：旧挂载被替换为新的 330102。
    assert await _count(session_factory, KnowledgeRegion, knowledge_id="K-1023") == 1


def test_content_fingerprint_ignores_status_and_regions() -> None:
    """指纹只覆盖影响向量的字段：改 status / regions 不变，改 content 则变。"""
    base = _item()
    assert base.content_fingerprint() == _item(status=KnowledgeStatus.DRAFT).content_fingerprint()
    assert base.content_fingerprint() == _item(regions=[_AREA]).content_fingerprint()
    assert base.content_fingerprint() != _item(content="换了正文").content_fingerprint()
    assert base.content_fingerprint() != _item(title="换了标题").content_fingerprint()


async def test_mock_embedder_is_deterministic() -> None:
    """MockEmbedder：同文同向量、异文异向量、维度固定、L2 归一化。"""
    embedder = MockEmbedder()
    same_a, same_b, other = await embedder.embed(["水稻稻飞虱", "水稻稻飞虱", "茶园茶饼病"])

    assert same_a == same_b
    assert same_a != other
    assert len(same_a) == EMBEDDING_DIM
    assert math.isclose(sum(v * v for v in same_a), 1.0, rel_tol=1e-9)


async def test_wrong_dimension_embedder_raises(session_factory) -> None:
    """向 `_upsert_one` 注入返回 768 维的向量实现时抛 `IndexBuildError`。"""

    class _Dim768:
        dimensions = 768

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 768 for _ in texts]

    async with session_factory() as db:
        indexer = KnowledgeIndexer(db, _Dim768())
        with pytest.raises(IndexBuildError):
            await indexer._upsert_one(_item(), None)  # noqa: SLF001  验收要求直接注入断言


async def test_wrong_dimension_collected_into_errors(session_factory) -> None:
    """整轮构建时维度错误被收集进 `errors` 而非中断：一次能报出全部问题条目。"""

    class _Dim768:
        dimensions = 768

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 768 for _ in texts]

    report = await _build(session_factory, [_item(), _pesticide()], embedder=_Dim768())

    assert report.upserted == 0
    assert len(report.errors) == 2
    assert await _count(session_factory, KnowledgeItem) == 0


async def test_mock_mode_issues_no_network_request(session_factory, respx_mock: respx.Router) -> None:
    """`USE_MOCK_LLM=true` 下构建不发起任何真实向量化请求。"""
    embedder = build_embedder()
    assert isinstance(embedder, MockEmbedder)

    report = await _build(session_factory, [_item()], embedder=embedder)

    assert report.upserted == 1
    assert respx_mock.calls == []


def test_embedding_text_excludes_pesticide_fields() -> None:
    """向量输入是 `title\\ncontent`，不含剂量/登记作物等元数据：语义信号不被稀释。"""
    item = _pesticide()
    text = item.embedding_text()

    assert text == f"{item.title}\n{item.content}"
    assert item.dosage_unit not in text
    assert "registered_crops" not in text


async def test_effective_and_expire_dates_roundtrip(session_factory) -> None:
    """生效期与失效期原样落库，供检索侧按日期过滤。"""
    await _build(session_factory, [_item(effective_date=date(2025, 3, 1), expire_date=date(2027, 12, 31))])

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()
    assert row.effective_date == date(2025, 3, 1)
    assert row.expire_date == date(2027, 12, 31)
