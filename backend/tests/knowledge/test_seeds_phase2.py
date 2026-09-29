"""T2-53 验收：二期油菜与通用兜底两线的知识条目。

这两条线的共同点是"能力弱于一期五条流程"，设计重点不是内容量而是"在知识不足时不越界"：
油菜不得放行药剂条目（RAPE-01），通用不得存在专属阈值与药剂条目。本文件既断言数据文件
满足这两条边界，也断言构建器把边界变成"构建时会拦住的事实"。

一部分用例跑在真实 PostgreSQL 上：直接向库里插入一条"误置 approved 的油菜药剂条目"，
验证 T2-18 的 `kind_filter_for` 在候选集层面就把它挡掉——这道防线是为"调研补齐过程中
误操作"准备的，单靠数据文件约束不够稳。
"""

import json
from importlib.resources import files

import pytest
from sqlalchemy import func, select

from app.agent.contracts import EvidenceQuery
from app.agent.state import EvidenceKind, ProcessId
from app.knowledge.embedding import MockEmbedder
from app.knowledge.indexer import (
    SEEDS_PACKAGE,
    KnowledgeIndexer,
    flow_constraint_error,
    load_seed_dir,
)
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schema import KnowledgeRegionSpec, KnowledgeSeedItem
from app.knowledge.status import KnowledgeStatus
from app.models.knowledge import KnowledgeItem
from app.process_config.loader import get_process_config

PHASE2_FILES = ("general.jsonl", "rapeseed.jsonl")
NATIONAL_MOUNT = [KnowledgeRegionSpec(region_code="000000", region_level="national")]


def _load(filename: str) -> list[KnowledgeSeedItem]:
    """按文件名解析单份 jsonl，返回校验通过的条目。"""
    path = files(SEEDS_PACKAGE).joinpath(filename)
    text = path.read_text(encoding="utf-8")
    return [KnowledgeSeedItem.model_validate(json.loads(line)) for line in text.splitlines() if line.strip()]


# ---- 数据文件的结构与边界 ----


def test_phase2_files_parse() -> None:
    """两份文件都能解析且非空。"""
    for name in PHASE2_FILES:
        assert _load(name), f"{name} 解析结果为空"


def test_phase2_process_ids_are_rapeseed_and_general() -> None:
    """两条线的 `process_id` 分别为 `rapeseed` 与 `general`。"""
    assert {item.process_id for item in _load("rapeseed.jsonl")} == {ProcessId.RAPESEED}
    assert {item.process_id for item in _load("general.jsonl")} == {ProcessId.GENERAL}


def test_rapeseed_has_no_approved_pesticide() -> None:
    """油菜文件中不存在 `status=approved` 且 `kind=pesticide` 的条目——RAPE-01 的数据侧保障。"""
    offenders = [
        item.knowledge_id
        for item in _load("rapeseed.jsonl")
        if item.kind is EvidenceKind.PESTICIDE and item.status is KnowledgeStatus.APPROVED
    ]

    assert offenders == []


def test_rapeseed_has_draft_pesticide_reference() -> None:
    """油菜文件存在 `kind=pesticide` 但 `status=draft` 的条目，证明"录入但不放行"的路径成立。"""
    drafts = [
        item
        for item in _load("rapeseed.jsonl")
        if item.kind is EvidenceKind.PESTICIDE and item.status is KnowledgeStatus.DRAFT
    ]

    assert drafts


def test_rapeseed_has_sclerotinia_cultivation_entry() -> None:
    """油菜文件存在 `kind=cultivation` 且标题含"菌核病"的条目。"""
    entries = [
        item for item in _load("rapeseed.jsonl") if item.kind is EvidenceKind.CULTIVATION and "菌核病" in item.title
    ]

    assert entries


def test_general_has_no_threshold() -> None:
    """通用文件中不存在 `kind=threshold` 条目（通用流程无专属阈值）。"""
    assert [item.knowledge_id for item in _load("general.jsonl") if item.kind is EvidenceKind.THRESHOLD] == []


def test_general_has_no_pesticide() -> None:
    """通用文件中不存在 `kind=pesticide` 条目（通用流程无药剂条目）。"""
    assert [item.knowledge_id for item in _load("general.jsonl") if item.kind is EvidenceKind.PESTICIDE] == []


def test_general_items_all_mounted_nationally() -> None:
    """通用条目一律挂全国 `000000`：挂到具体地区会在未挂载地区失去兜底作用。"""
    for item in _load("general.jsonl"):
        assert item.regions, item.knowledge_id
        assert all(spec.region_code == "000000" and spec.region_level == "national" for spec in item.regions), (
            item.knowledge_id
        )


def test_general_items_are_approved_and_loadable() -> None:
    """通用条目可载入且为 `approved`：它们是任意流程的回退兜底，必须可被检索。"""
    items = _load("general.jsonl")

    assert items
    assert all(item.status is KnowledgeStatus.APPROVED for item in items)


# ---- 构建期流程级约束 ----


def test_flow_constraint_rejects_general_threshold() -> None:
    """通用流程出现 `threshold` 条目时约束函数报错且消息含 `knowledge_id`。"""
    item = KnowledgeSeedItem(
        knowledge_id="K-GEN-BAD",
        process_id=ProcessId.GENERAL,
        kind=EvidenceKind.THRESHOLD,
        title="通用阈值（不应存在）",
        content="不应存在的阈值条目。",
        status=KnowledgeStatus.APPROVED,
        regions=NATIONAL_MOUNT,
    )

    error = flow_constraint_error(item)

    assert error is not None
    assert "K-GEN-BAD" in error


def test_flow_constraint_rejects_general_pesticide() -> None:
    """通用流程出现 `pesticide` 条目时同样被拦下。"""
    item = KnowledgeSeedItem(
        knowledge_id="K-GEN-BAD2",
        process_id=ProcessId.GENERAL,
        kind=EvidenceKind.PESTICIDE,
        title="通用药剂（不应存在）",
        content="不应存在的药剂条目。",
        pesticide_name="测试药",
        dosage_min=1.0,
        dosage_max=2.0,
        dosage_unit="克/亩",
        safety_interval_days=7,
        status=KnowledgeStatus.APPROVED,
        regions=NATIONAL_MOUNT,
    )

    assert flow_constraint_error(item) is not None


def test_flow_constraint_rejects_approved_rapeseed_pesticide() -> None:
    """油菜流程出现 `approved` 的 `pesticide` 条目时被拦下（调研补齐前不放行）。"""
    item = KnowledgeSeedItem(
        knowledge_id="K-RAPE-BAD",
        process_id=ProcessId.RAPESEED,
        kind=EvidenceKind.PESTICIDE,
        title="油菜药剂（不应放行）",
        content="不应放行的药剂条目。",
        pesticide_name="测试药",
        dosage_min=1.0,
        dosage_max=2.0,
        dosage_unit="克/亩",
        safety_interval_days=7,
        status=KnowledgeStatus.APPROVED,
        regions=NATIONAL_MOUNT,
    )

    error = flow_constraint_error(item)

    assert error is not None
    assert "K-RAPE-BAD" in error


def test_flow_constraint_allows_rapeseed_draft_pesticide() -> None:
    """油菜的 `draft` 药剂条目是允许的：录入但不放行。"""
    item = KnowledgeSeedItem(
        knowledge_id="K-RAPE-OK",
        process_id=ProcessId.RAPESEED,
        kind=EvidenceKind.PESTICIDE,
        title="油菜药剂（占位）",
        content="占位条目。",
        pesticide_name="待确认",
        dosage_min=0.0,
        dosage_max=0.0,
        dosage_unit="克/亩",
        safety_interval_days=0,
        status=KnowledgeStatus.DRAFT,
        regions=NATIONAL_MOUNT,
    )

    assert flow_constraint_error(item) is None


def test_flow_constraint_passes_real_seed_data() -> None:
    """真实的两份数据文件全部通过流程级约束——文件与构建器约束一致。"""
    offenders = [
        item.knowledge_id for item in [*_load("general.jsonl"), *_load("rapeseed.jsonl")] if flow_constraint_error(item)
    ]

    assert offenders == []


@pytest.mark.usefixtures("requires_postgres")
async def test_build_reports_error_with_knowledge_id_for_general_threshold(session_factory) -> None:
    """人工插入一条通用 `threshold` 条目时，构建报错且错误消息含 `knowledge_id`。"""
    bad = KnowledgeSeedItem(
        knowledge_id="K-GEN-BAD3",
        process_id=ProcessId.GENERAL,
        kind=EvidenceKind.THRESHOLD,
        title="通用阈值（不应存在）",
        content="不应存在的阈值条目。",
        status=KnowledgeStatus.APPROVED,
        regions=NATIONAL_MOUNT,
    )

    async with session_factory() as db:
        report = await KnowledgeIndexer(db, MockEmbedder()).build([bad], check_process=False)
        await db.commit()

    assert report.errors
    assert any("K-GEN-BAD3" in message for message in report.errors)


# ---- 检索联动：油菜药剂被候选集排除、通用条目可兜底 ----


def _item_row(**overrides) -> KnowledgeItem:
    """构造一条可检索条目（直接落 ORM，绕过数据文件的流程级约束）。"""
    payload: dict = {
        "knowledge_id": "K-ROW",
        "process_id": ProcessId.RAPESEED.value,
        "kind": EvidenceKind.CULTIVATION.value,
        "title": "菌核病防治窗口",
        "content": "花期连续阴雨注意排水。",
        "registered_crops": [],
        "forbidden_combinations": [],
        "region_scope": None,
        "status": "approved",
        "embedding": MockEmbedder()._unit_vector("菌核病"),  # noqa: SLF001  测试直接用确定性向量
    }
    payload.update(overrides)
    return KnowledgeItem(**payload)


async def _insert(session_factory, *items: KnowledgeItem) -> None:
    async with session_factory() as db:
        for item in items:
            db.add(item)
        await db.commit()


@pytest.mark.usefixtures("requires_postgres")
async def test_rapeseed_query_excludes_pesticide_kind(session_factory) -> None:
    """油菜流程即使库里存在被误置 `approved` 的药剂条目，`kind_filter` 也在候选集层面排除它。

    这条模拟"调研补齐过程中误操作把油菜药剂条目置 approved"的场景：数据文件约束拦不住
    已落库的旧数据，T2-18 的过滤就是第二道防线。
    """
    vector = MockEmbedder()._unit_vector("菌核病")  # noqa: SLF001
    await _insert(
        session_factory,
        _item_row(knowledge_id="K-rape-ok", kind="cultivation", title="菌核病防治窗口", embedding=vector),
        _item_row(
            knowledge_id="K-rape-bad",
            kind="pesticide",
            title="油菜药剂（误置）",
            pesticide_name="测试药",
            embedding=vector,
        ),
    )
    config = get_process_config(ProcessId.RAPESEED)
    kind_filter = ["pest", "disease", "cultivation", "threshold"]  # T2-18 kind_filter_for 的油菜口径
    repo = KnowledgeRepository(session_factory, MockEmbedder())

    results = await repo.search(
        EvidenceQuery(
            process_id=ProcessId.RAPESEED,
            keywords=["菌核病"],
            kind_filter=kind_filter,
            top_k=8,
        )
    )

    kinds = {item.kind for item in results}
    assert config.process_id is ProcessId.RAPESEED
    assert "pesticide" not in kinds
    assert "K-rape-ok" in {item.knowledge_id for item in results}


@pytest.mark.usefixtures("requires_postgres")
async def test_general_entry_backfills_other_process(session_factory) -> None:
    """以 `process_id=rice` 查询时能召回 `general` 兜底条目（当该流程无专属结果）。"""
    vector = MockEmbedder()._unit_vector("施肥")  # noqa: SLF001
    await _insert(
        session_factory,
        _item_row(
            knowledge_id="K-gen-fertilize",
            process_id="general",
            kind="cultivation",
            title="施肥通用原则",
            content="施肥应测土配方。",
            embedding=vector,
        ),
    )
    repo = KnowledgeRepository(session_factory, MockEmbedder())

    results = await repo.search(EvidenceQuery(process_id=ProcessId.RICE, keywords=["施肥"], top_k=8))

    assert "K-gen-fertilize" in {item.knowledge_id for item in results}


# ---- 载入：二期两份文件被 `load_seed_dir` 纳入 ----


def test_load_seed_dir_includes_phase2_files() -> None:
    """`load_seed_dir` 通配读取目录，二期两份文件被自动纳入且总数大于一期五份之和。"""
    phase1 = sum(len(_load(name)) for name in ("chili.jsonl", "pig.jsonl", "rice.jsonl", "tea.jsonl", "weed.jsonl"))
    phase2 = sum(len(_load(name)) for name in PHASE2_FILES)

    all_items = load_seed_dir()
    ids = {item.knowledge_id for item in all_items}

    assert len(all_items) >= phase1 + phase2
    assert "K-GEN-0001" in ids
    assert "K-RAPE-0101" in ids


@pytest.mark.usefixtures("requires_postgres")
async def test_full_build_persists_general_and_rapeseed(session_factory) -> None:
    """全量构建（含二期）无流程级错误，且通用与油菜条目均按状态落库。"""
    items = load_seed_dir()

    async with session_factory() as db:
        report = await KnowledgeIndexer(db, MockEmbedder()).build(items, check_process=True)
        await db.commit()

    assert report.errors == []

    async with session_factory() as db:
        stmt = (
            select(func.count()).select_from(KnowledgeItem).where(KnowledgeItem.process_id.in_(["general", "rapeseed"]))
        )
        stored = (await db.execute(stmt)).scalar_one()

    assert stored == sum(len(_load(name)) for name in PHASE2_FILES)
