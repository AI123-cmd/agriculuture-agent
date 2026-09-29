"""T2-47 验收：知识条目表结构、状态语义与向量维度。

跑在真实 PostgreSQL 上：`vector(1024)`、`JSONB`、CHECK 约束与 HNSW 索引都不是 SQLite
能力范围内的对象。表由迁移 `0006_knowledge` 建出（`migrated_db`），因此本用例同时验证了
"迁移与 ORM 一致"这一前提。

覆盖五条任务文件点名的断言：剂量区间倒挂被约束拦下、`suspended` 缺理由被拦下、
`suspended` 带理由可插入、向量 1024 维可写而 768 维报错、业务主键允许 `K-1023` 形态。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.knowledge.status import SEARCHABLE_STATUSES, KnowledgeStatus
from app.models import KnowledgeItem, KnowledgeRegion
from app.repositories.knowledge import KnowledgeRepository

pytestmark = pytest.mark.usefixtures("requires_postgres")


def _item(**overrides) -> KnowledgeItem:
    """一条合法的药剂类条目，逐项覆盖时只改关心的字段。"""
    base = {
        "knowledge_id": "K-1023",
        "process_id": "rice",
        "kind": "pesticide",
        "title": "吡虫啉防治稻飞虱",
        "content": "若虫高峰期施药。",
        "pesticide_name": "吡虫啉",
        "registered_crops": ["水稻"],
        "dosage_min": Decimal("10.0000"),
        "dosage_max": Decimal("30.0000"),
        "dosage_unit": "克/亩",
        "safety_interval_days": 7,
        "max_uses_per_season": 2,
        "forbidden_combinations": [],
        "status": "approved",
    }
    base.update(overrides)
    return KnowledgeItem(**base)


async def _insert(session_factory, obj: KnowledgeItem) -> None:
    async with session_factory() as db:
        db.add(obj)
        await db.commit()


def test_status_values_are_four() -> None:
    """四值闭集，顺序与任务文件一致。"""
    assert [s.value for s in KnowledgeStatus] == ["draft", "approved", "suspended", "deprecated"]


def test_searchable_statuses_is_approved_only() -> None:
    """可检索集合恰为 `{approved}`，`suspended` 与 `deprecated` 都在集合外。"""
    assert SEARCHABLE_STATUSES == {KnowledgeStatus.APPROVED}
    assert KnowledgeStatus.SUSPENDED not in SEARCHABLE_STATUSES
    assert KnowledgeStatus.DEPRECATED not in SEARCHABLE_STATUSES
    assert KnowledgeStatus.DRAFT not in SEARCHABLE_STATUSES


async def test_knowledge_id_accepts_business_form(session_factory) -> None:
    """主键允许 `K-1023` 这类业务编码形态（不是 UUID）。"""
    await _insert(session_factory, _item())

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()

    assert row.knowledge_id == "K-1023"
    assert row.kind == "pesticide"
    assert row.registered_crops == ["水稻"]
    assert row.dosage_min == Decimal("10.0000")


async def test_dosage_range_inverted_rejected(session_factory) -> None:
    """`dosage_min > dosage_max` 必须被 `ck_knowledge_dosage_range` 拦下。"""
    with pytest.raises(IntegrityError):
        await _insert(session_factory, _item(dosage_min=Decimal("40.0000"), dosage_max=Decimal("10.0000")))


async def test_suspended_without_reason_rejected(session_factory) -> None:
    """`suspended` 且 `status_reason` 为空必须被 `ck_knowledge_suspended_reason` 拦下。"""
    with pytest.raises(IntegrityError):
        await _insert(session_factory, _item(status="suspended", status_reason=None))


async def test_suspended_with_reason_ok(session_factory) -> None:
    """`suspended` 带理由可正常插入——这是抗药性暂停名单的表达方式。"""
    await _insert(session_factory, _item(status="suspended", status_reason="稻飞虱已产生抗性，暂停推荐"))

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()

    assert row.status == "suspended"
    assert row.status_reason == "稻飞虱已产生抗性，暂停推荐"


async def test_embedding_1024_dim_ok(session_factory) -> None:
    """1024 维向量可写入（与 `Settings.EMBEDDING_DIM` 一致）。"""
    await _insert(session_factory, _item(embedding=[0.1] * 1024))

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()

    assert row.embedding is not None
    assert len(row.embedding) == 1024


async def test_embedding_768_dim_rejected(session_factory) -> None:
    """768 维向量与列的 1024 维不匹配，必须报错而不是静默截断。"""
    with pytest.raises(DBAPIError):
        await _insert(session_factory, _item(embedding=[0.1] * 768))


async def test_repository_get_by_ids_and_by_status(session_factory) -> None:
    """基础读取：按 id 批量取、按状态取（默认只放行 approved）。"""
    await _insert(session_factory, _item(knowledge_id="K-1", status="approved"))
    await _insert(session_factory, _item(knowledge_id="K-2", status="draft"))

    async with session_factory() as db:
        repo = KnowledgeRepository(db)
        batch = await repo.get_by_ids(["K-1", "K-2", "K-404"])
        approved = await repo.list_by_status()
        drafts = await repo.list_by_status(KnowledgeStatus.DRAFT)
        searchable = await repo.list_searchable()
        empty = await repo.get_by_ids([])

    assert {r.knowledge_id for r in batch} == {"K-1", "K-2"}
    assert {r.knowledge_id for r in approved} == {"K-1"}
    assert {r.knowledge_id for r in drafts} == {"K-2"}
    assert {r.knowledge_id for r in searchable} == {"K-1"}
    assert empty == []


async def test_knowledge_region_unique_per_entry(session_factory) -> None:
    """同一条目同一行政区只能挂一行（`uq_knowledge_region`）。"""
    await _insert(session_factory, _item())
    await _insert(
        session_factory,
        KnowledgeRegion(id="r1", knowledge_id="K-1023", region_code="330102", region_level="county"),
    )

    with pytest.raises(IntegrityError):
        await _insert(
            session_factory,
            KnowledgeRegion(id="r2", knowledge_id="K-1023", region_code="330102", region_level="county"),
        )


async def test_knowledge_region_multiple_levels(session_factory) -> None:
    """同一条目可挂多个行政区、多级（县级 + 全国兜底）。"""
    await _insert(session_factory, _item())
    for idx, (code, level) in enumerate([("330102", "county"), ("330000", "city"), ("000000", "national")], start=1):
        await _insert(
            session_factory,
            KnowledgeRegion(id=f"r{idx}", knowledge_id="K-1023", region_code=code, region_level=level),
        )

    async with session_factory() as db:
        rows = (
            (await db.execute(select(KnowledgeRegion).where(KnowledgeRegion.knowledge_id == "K-1023"))).scalars().all()
        )

    assert {r.region_code for r in rows} == {"330102", "330000", "000000"}


async def test_expire_date_roundtrips(session_factory) -> None:
    """适用期用日期列表达，能原样取回。"""
    await _insert(session_factory, _item(effective_date=date(2025, 3, 1), expire_date=date(2027, 3, 1)))

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()

    assert row.effective_date == date(2025, 3, 1)
    assert row.expire_date == date(2027, 3, 1)
