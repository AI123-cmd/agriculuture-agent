"""T2-54 验收：知识条目核准流程与状态机。

跑在真实 PostgreSQL 上：变更记录表在独立 `knowledge` schema 下，且"日志与主表同事务"
这条不变量依赖真实事务语义，SQLite 无法承载。表由迁移建出（`migrated_db`），因此本用例
同时验证了"迁移与 ORM 一致"这一前提。

覆盖任务文件点名的断言：合法迁移与状态落库、`suspended` 理由双写、非法迁移抛
`IllegalTransition`、缺理由抛 `ReasonRequired`、`deprecated` 终态、连续同动作第二次失败、
`operator_id` 为空被拒、每次成功迁移恰好写一条日志、构建器重跑不改状态、日志仓储只追加。
"""

import inspect
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.knowledge.embedding import MockEmbedder
from app.knowledge.errors import IllegalTransition, KnowledgeNotFound, OperatorRequired, ReasonRequired
from app.knowledge.indexer import KnowledgeIndexer
from app.knowledge.schema import KnowledgeSeedItem
from app.knowledge.status import KnowledgeStatus
from app.models import KnowledgeItem, KnowledgeReviewLog
from app.observability import metrics
from app.repositories.knowledge_review import KnowledgeReviewLogRepository
from app.services.knowledge_review import (
    ALLOWED_TRANSITIONS,
    REVIEW_METRIC,
    KnowledgeAction,
    KnowledgeReviewService,
)

pytestmark = pytest.mark.usefixtures("requires_postgres")

_OPERATOR = "user-operator-001"


@pytest.fixture(autouse=True)
def _reset_metrics():
    """指标是进程内单例，用例之间必须清零，否则动作计数会累积。"""
    metrics.reset()
    yield
    metrics.reset()


def _item_model(knowledge_id: str = "K-1023", status: str = "draft", **overrides) -> KnowledgeItem:
    base = {
        "knowledge_id": knowledge_id,
        "process_id": "rice",
        "kind": "pest",
        "title": "稻飞虱识别与防治",
        "content": "稻飞虱聚集在稻丛基部吸食汁液。",
        "status": status,
    }
    base.update(overrides)
    return KnowledgeItem(**base)


async def _seed(session_factory, item: KnowledgeItem) -> None:
    async with session_factory() as db:
        db.add(item)
        await db.commit()


async def _status(session_factory, knowledge_id: str = "K-1023") -> str:
    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == knowledge_id))).scalar_one()
        return row.status


async def _reason(session_factory, knowledge_id: str = "K-1023") -> str | None:
    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == knowledge_id))).scalar_one()
        return row.status_reason


async def _logs(session_factory, knowledge_id: str = "K-1023") -> list[KnowledgeReviewLog]:
    async with session_factory() as db:
        repo = KnowledgeReviewLogRepository(db)
        return await repo.list_for_knowledge(knowledge_id)


async def _apply(session_factory, knowledge_id: str, action: KnowledgeAction, **kwargs) -> KnowledgeItem:
    kwargs.setdefault("operator_id", _OPERATOR)
    async with session_factory() as db:
        service = KnowledgeReviewService(db)
        return await service.apply(knowledge_id=knowledge_id, action=action, **kwargs)


# ---- 合法迁移 ----


async def test_draft_approve_becomes_approved(session_factory) -> None:
    """`draft` 提交核准后为 `approved`。"""
    await _seed(session_factory, _item_model(status="draft"))

    item = await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE)

    assert item.status == KnowledgeStatus.APPROVED.value
    assert await _status(session_factory) == "approved"


async def test_approved_suspend_writes_status_reason(session_factory) -> None:
    """`approved` 暂停后为 `suspended`，且 `status_reason` 写入主表与日志。"""
    await _seed(session_factory, _item_model(status="approved"))

    item = await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND, reason="抗药性暂停")

    assert item.status == KnowledgeStatus.SUSPENDED.value
    assert await _reason(session_factory) == "抗药性暂停"
    logs = await _logs(session_factory)
    assert logs[-1].reason == "抗药性暂停"


async def test_suspended_resume_becomes_approved_and_clears_reason(session_factory) -> None:
    """`suspended` 恢复后为 `approved`，主表旧暂停理由被清空（当前状态与说明一致）。"""
    await _seed(session_factory, _item_model(status="suspended", status_reason="旧的暂停理由"))

    item = await _apply(session_factory, "K-1023", KnowledgeAction.RESUME)

    assert item.status == KnowledgeStatus.APPROVED.value
    assert await _reason(session_factory) is None
    # 历史仍保留在变更记录里。
    logs = await _logs(session_factory)
    assert logs[-1].from_status == "suspended"
    assert logs[-1].to_status == "approved"


async def test_approved_deprecate_becomes_deprecated(session_factory) -> None:
    """`approved` 废弃后为 `deprecated`。"""
    await _seed(session_factory, _item_model(status="approved"))

    item = await _apply(session_factory, "K-1023", KnowledgeAction.DEPRECATE, reason="条目被替代")

    assert item.status == KnowledgeStatus.DEPRECATED.value


async def test_submit_keeps_draft(session_factory) -> None:
    """`submit` 不引入第五个状态：`draft` 提交待审后仍是 `draft`。"""
    await _seed(session_factory, _item_model(status="draft"))

    item = await _apply(session_factory, "K-1023", KnowledgeAction.SUBMIT)

    assert item.status == KnowledgeStatus.DRAFT.value


async def test_suspended_deprecate_is_legal(session_factory) -> None:
    """`suspended` 也可直接废弃——暂停名单里的条目同样需要下线。"""
    await _seed(session_factory, _item_model(status="suspended", status_reason="暂停中"))

    item = await _apply(session_factory, "K-1023", KnowledgeAction.DEPRECATE, reason="确认下线")

    assert item.status == KnowledgeStatus.DEPRECATED.value


# ---- 非法迁移 ----


async def test_draft_suspend_raises_illegal_transition(session_factory) -> None:
    """`draft` 直接 `suspend` 是越级迁移，抛 `IllegalTransition`（409）。"""
    await _seed(session_factory, _item_model(status="draft"))

    with pytest.raises(IllegalTransition) as exc:
        await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND, reason="理由给了也应拒")

    assert exc.value.code == 409
    assert await _status(session_factory) == "draft"
    assert await _logs(session_factory) == []


async def test_second_approve_raises_illegal_transition(session_factory) -> None:
    """对已 `approved` 的条目再次 `approve`：第二次抛 `IllegalTransition`。"""
    await _seed(session_factory, _item_model(status="draft"))
    await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE)

    with pytest.raises(IllegalTransition):
        await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE)


async def test_deprecated_is_terminal(session_factory) -> None:
    """`deprecated` 是终态，无法再迁移到任何状态。"""
    assert ALLOWED_TRANSITIONS[KnowledgeStatus.DEPRECATED] == {}

    await _seed(session_factory, _item_model(status="deprecated"))
    for action in KnowledgeAction:
        with pytest.raises(IllegalTransition):
            await _apply(session_factory, "K-1023", action)


async def test_approved_submit_is_illegal(session_factory) -> None:
    """`approved` 上没有 `submit` 动作——越级/错误动作一律拒绝。"""
    await _seed(session_factory, _item_model(status="approved"))

    with pytest.raises(IllegalTransition):
        await _apply(session_factory, "K-1023", KnowledgeAction.SUBMIT)


# ---- 理由校验 ----


async def test_suspend_without_reason_raises(session_factory) -> None:
    """`suspend` 缺理由抛 `ReasonRequired`（400），且不落库、不写日志。"""
    await _seed(session_factory, _item_model(status="approved"))

    with pytest.raises(ReasonRequired) as exc:
        await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND)

    assert exc.value.code == 400
    assert await _status(session_factory) == "approved"
    assert await _logs(session_factory) == []


async def test_suspend_with_whitespace_reason_raises(session_factory) -> None:
    """只有空白的理由等同没给。"""
    await _seed(session_factory, _item_model(status="approved"))

    with pytest.raises(ReasonRequired):
        await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND, reason="   ")


async def test_reject_without_reason_raises(session_factory) -> None:
    """`reject` 打回必须带理由。"""
    await _seed(session_factory, _item_model(status="draft"))

    with pytest.raises(ReasonRequired):
        await _apply(session_factory, "K-1023", KnowledgeAction.REJECT)


async def test_approve_and_resume_without_reason_ok(session_factory) -> None:
    """`approve` 与 `resume` 可无理由——只有"收紧"动作才强制理由。"""
    await _seed(session_factory, _item_model(status="draft"))
    await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE)

    await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND, reason="暂停")
    item = await _apply(session_factory, "K-1023", KnowledgeAction.RESUME)

    assert item.status == KnowledgeStatus.APPROVED.value


# ---- 操作人与审计 ----


async def test_empty_operator_id_rejected(session_factory) -> None:
    """`operator_id` 为空时抛 `OperatorRequired`，不得产生任何变更。"""
    await _seed(session_factory, _item_model(status="draft"))

    with pytest.raises(OperatorRequired) as exc:
        await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE, operator_id="")

    assert exc.value.code == 400
    assert await _status(session_factory) == "draft"
    assert await _logs(session_factory) == []


async def test_not_found_raises(session_factory) -> None:
    """条目不存在抛 `KnowledgeNotFound`（404）。"""
    with pytest.raises(KnowledgeNotFound) as exc:
        await _apply(session_factory, "K-404", KnowledgeAction.APPROVE)

    assert exc.value.code == 404


async def test_each_successful_transition_writes_one_log(session_factory) -> None:
    """每次成功迁移写一条 `knowledge_review_log`，且字段完整。"""
    await _seed(session_factory, _item_model(status="draft"))

    await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE)
    await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND, reason="暂停理由", consulted_ref="sess-1")

    logs = await _logs(session_factory)
    assert len(logs) == 2
    first, second = logs
    assert (first.from_status, first.to_status, first.action) == ("draft", "approved", "approve")
    assert first.operator_id == _OPERATOR
    assert (second.from_status, second.to_status, second.action) == ("approved", "suspended", "suspend")
    assert second.reason == "暂停理由"
    assert second.consulted_ref == "sess-1"


async def test_metric_incremented_per_action(session_factory) -> None:
    """每次成功变更上报一条指标（各动作分别计数），供"暂停名单是否异常频繁变动"监控。"""
    await _seed(session_factory, _item_model(status="draft"))

    await _apply(session_factory, "K-1023", KnowledgeAction.APPROVE)
    await _apply(session_factory, "K-1023", KnowledgeAction.SUSPEND, reason="暂停")

    assert metrics.counter_value(REVIEW_METRIC, tags={"action": "approve"}) == 1
    assert metrics.counter_value(REVIEW_METRIC, tags={"action": "suspend"}) == 1


# ---- 与索引构建的衔接 ----


async def test_rebuild_does_not_change_existing_status(session_factory, caplog) -> None:
    """构建器重跑不改已有条目状态：库中 `draft`、文件中 `approved`，重跑后仍为 `draft` 并 warn。"""
    await _seed(session_factory, _item_model(status="draft", content_hash="stale"))

    seed = KnowledgeSeedItem.model_validate(
        {
            "knowledge_id": "K-1023",
            "process_id": "rice",
            "kind": "pest",
            "title": "稻飞虱识别与防治",
            "content": "改写后的正文，触发一次增量更新。",
            "status": KnowledgeStatus.APPROVED,
        }
    )

    with caplog.at_level(logging.WARNING, logger="app.knowledge.indexer"):
        async with session_factory() as db:
            report = await KnowledgeIndexer(db, MockEmbedder()).build([seed], check_process=False, dry_run=False)
            await db.commit()

    assert report.upserted == 1
    assert await _status(session_factory) == "draft"
    assert any("不一致" in r.message for r in caplog.records)


async def test_rebuild_updates_content_but_not_status(session_factory) -> None:
    """增量更新改内容字段、不改状态——两条写路径互不干扰。"""
    await _seed(session_factory, _item_model(status="approved", content="旧正文", content_hash="stale"))

    seed = KnowledgeSeedItem.model_validate(
        {
            "knowledge_id": "K-1023",
            "process_id": "rice",
            "kind": "pest",
            "title": "稻飞虱识别与防治",
            "content": "新正文",
            "status": KnowledgeStatus.DRAFT,
        }
    )
    async with session_factory() as db:
        await KnowledgeIndexer(db, MockEmbedder()).build([seed], check_process=False)
        await db.commit()

    async with session_factory() as db:
        row = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == "K-1023"))).scalar_one()
    assert row.content == "新正文"
    assert row.status == "approved"


# ---- 日志仓储不可变（反射断言）----


def test_log_repository_has_no_mutation_methods() -> None:
    """反射断言：日志仓储只有追加与查询方法，不存在更新/删除方法。"""
    method_names = {name for name, _ in inspect.getmembers(KnowledgeReviewLogRepository, inspect.isfunction)}
    forbidden = {"update", "delete", "modify", "remove", "save", "put", "set_status"}
    assert method_names & forbidden == set(), f"日志仓储不得有变更方法：{method_names & forbidden}"
    # 正向确认关键方法存在，避免"方法全被删了"也能通过上面的断言。
    assert {"insert", "list_for_knowledge", "list_by_operator"} <= method_names


def test_log_model_has_no_updated_at() -> None:
    """变更记录表不带 `updated_at`：不可变性由结构表达（无更新时间列）。"""
    assert "updated_at" not in KnowledgeReviewLog.__table__.columns
