"""T2-52 验收：一期五条流程的知识条目数据文件。

本文件对 `app/knowledge/seeds/` 下五份 jsonl 逐条做结构与内容断言。刻意**逐个文件解析**
而不是 `load_seed_dir()` 全量：后者会把后续任务（T2-53）新增的文件也带进来，导致"process_id
必须属于一期五条流程"这类断言在二期内容加入后误报。按文件名取样让本用例只约束自己这一期的
产物。

另一部分用例跑在真实 PostgreSQL 上：把五份文件构建入库，验证"文件里的 approved 条目数"与
"库里的 approved 行数"一致——这是数据文件与索引构建链路对得上的最终证据。
"""

import json
from importlib.resources import files

import pytest
from sqlalchemy import func, select

from app.agent.state import EvidenceKind, ProcessId
from app.knowledge.embedding import MockEmbedder
from app.knowledge.indexer import SEEDS_PACKAGE, BuildReport, KnowledgeIndexer
from app.knowledge.schema import KnowledgeSeedItem
from app.knowledge.status import KnowledgeStatus
from app.models.knowledge import KnowledgeItem

PHASE1_PROCESSES = (ProcessId.RICE, ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED, ProcessId.PIG)
PHASE1_FILES = ("chili.jsonl", "pig.jsonl", "rice.jsonl", "tea.jsonl", "weed.jsonl")


def _load(filename: str) -> list[KnowledgeSeedItem]:
    """按文件名解析单份 jsonl，返回校验通过的条目。"""
    path = files(SEEDS_PACKAGE).joinpath(filename)
    text = path.read_text(encoding="utf-8")
    return [KnowledgeSeedItem.model_validate(json.loads(line)) for line in text.splitlines() if line.strip()]


def _all_items() -> list[KnowledgeSeedItem]:
    return [item for name in PHASE1_FILES for item in _load(name)]


# ---- 结构：五份文件均可解析、流程合法、药剂字段完整 ----


def test_all_phase1_files_parse() -> None:
    """五份文件都能解析且非空：文件缺失、字段拼错、校验不过都会在此暴露。"""
    for name in PHASE1_FILES:
        assert _load(name), f"{name} 解析结果为空"


def test_every_process_id_in_phase1_scope() -> None:
    """每条条目的 `process_id` 都属于一期五条流程。"""
    scopes = {pid.value for pid in PHASE1_PROCESSES}
    offenders = sorted({item.process_id.value for item in _all_items()} - scopes)

    assert offenders == []


def test_pesticide_items_have_full_dosage_and_interval() -> None:
    """所有 `kind=pesticide` 条目具备完整剂量区间与 `safety_interval_days`。

    这是 `R-DOSAGE` / `R-INTERVAL` 的比对基准；模型层已强制，此处再对真实数据兜底一次。
    """
    for item in _all_items():
        if item.kind is not EvidenceKind.PESTICIDE:
            continue
        assert item.pesticide_name, item.knowledge_id
        assert item.dosage_min is not None and item.dosage_max is not None, item.knowledge_id
        assert item.dosage_unit, item.knowledge_id
        assert item.safety_interval_days is not None, item.knowledge_id


def test_suspended_items_have_reason() -> None:
    """所有 `status=suspended` 条目带 `status_reason`（与表上 CHECK 约束一致）。"""
    for item in _all_items():
        if item.status is KnowledgeStatus.SUSPENDED:
            assert item.status_reason, item.knowledge_id


def test_approved_items_have_region_mount() -> None:
    """每条 `approved` 条目至少有一个地区挂载（`regions` 或 `region_scope`），保证可被召回。"""
    for item in _all_items():
        if item.status is KnowledgeStatus.APPROVED:
            assert item.regions or item.region_scope, item.knowledge_id


# ---- 茶与辣椒：登记作物必须含当前作物 ----


def test_tea_pesticides_registered_for_tea() -> None:
    """茶流程全部药剂条目的 `registered_crops` 含"茶"——R-REGISTERED-CROP 的高频拦截点。"""
    for item in _load("tea.jsonl"):
        if item.kind is EvidenceKind.PESTICIDE:
            assert "茶" in item.registered_crops, item.knowledge_id


def test_chili_pesticides_registered_for_chili() -> None:
    """辣椒流程全部药剂条目的 `registered_crops` 含"辣椒"。"""
    for item in _load("chili.jsonl"):
        if item.kind is EvidenceKind.PESTICIDE:
            assert "辣椒" in item.registered_crops, item.knowledge_id


# ---- 各流程的关键特例 ----


def test_rice_has_resistance_suspension_entry() -> None:
    """水稻文件存在 `status=suspended` 且理由提及抗药性的条目（抗药性暂停名单）。"""
    suspended = [
        item
        for item in _load("rice.jsonl")
        if item.status is KnowledgeStatus.SUSPENDED and item.status_reason and "抗药性" in item.status_reason
    ]

    assert suspended


def test_weed_has_long_residual_restriction_entry() -> None:
    """杂草文件存在 `kind=pesticide` 且 `forbidden_combinations` 非空的条目（后茬长残留限制）。"""
    restricted = [
        item
        for item in _load("weed.jsonl")
        if item.kind is EvidenceKind.PESTICIDE and item.forbidden_combinations
    ]

    assert restricted


def test_pig_has_animal_epidemic_law_reference() -> None:
    """养猪文件存在 `source_ref` 提及《动物防疫法》的条目。"""
    referenced = [item for item in _load("pig.jsonl") if item.source_ref and "《动物防疫法》" in item.source_ref]

    assert referenced


def test_chili_confusable_entry_has_home_verifiable_criteria() -> None:
    """辣椒青枯病与枯萎病判据条目含"菌脓"或"维管束"这类可在家庭现场操作的鉴别点。"""
    entries = [item for item in _load("chili.jsonl") if "青枯病" in item.title and "枯萎病" in item.title]

    assert entries, "缺少青枯病与枯萎病的鉴别判据条目"
    for item in entries:
        assert "菌脓" in item.content or "维管束" in item.content, item.knowledge_id


def test_weed_confusable_titles_cover_configured_candidates() -> None:
    """杂草草相鉴别条目覆盖配置 `weed.yaml` 的候选词组，避免"配置有判据、知识库没条目"。"""
    candidates = {"稗草", "稻稗", "千金子", "菵草", "看麦娘", "节节麦", "雀麦"}
    titles = [item.title for item in _load("weed.jsonl")]

    missing = [name for name in candidates if not any(name in title for title in titles)]
    assert missing == []


# ---- 落库：构建后行数与文件 approved 条目数一致 ----


@pytest.mark.usefixtures("requires_postgres")
async def test_build_persists_all_approved_items(session_factory) -> None:
    """用 `MockEmbedder` 构建五份文件：库中 approved 行数与文件中 approved 条目数一致。"""
    items = _all_items()
    expected_approved = sum(1 for item in items if item.status is KnowledgeStatus.APPROVED)
    process_values = [pid.value for pid in PHASE1_PROCESSES]

    async with session_factory() as db:
        report = await KnowledgeIndexer(db, MockEmbedder()).build(items, check_process=True)
        await db.commit()

    assert isinstance(report, BuildReport)
    assert report.errors == []

    async with session_factory() as db:
        stmt = (
            select(func.count())
            .select_from(KnowledgeItem)
            .where(KnowledgeItem.status == "approved", KnowledgeItem.process_id.in_(process_values))
        )
        stored = (await db.execute(stmt)).scalar_one()

    assert stored == expected_approved
    assert stored > 0
