"""知识条目核准流程与状态机（T2-54，技术方案 9.2）。

条目必须经人工审核才能从 `draft` 变为 `approved`；`suspended` 的每次变更必须留下操作记录
与理由；状态迁移必须合法（不允许从 `draft` 直接跳到 `suspended` 之类的越级）。每个
`approved` 条目都是闸门规则的比对基准，因此"谁在什么时候把哪条改成了什么状态、理由是什么"
必须可查——本模块与 `knowledge_review_log` 一起落实这条要求。

**不设 `pending` 独立状态**：用 `draft` 承载"待审"，与方案 9.2 的四值保持完全一致。方案只
给了四值，若在这里加第五个状态，检索侧与闸门侧的所有 `status` 判断都要跟着改。审核队列可以
用 `knowledge_review_log` 查"最近提交待审的条目"来表达，不占用主表的状态位。

**核准必须人工**：`draft -> approved` 不允许由自动流程触发。索引构建器写入条目时只能用数据
文件里预先声明的状态，不能调用 `apply` 自行核准。这条边界保证"未经人工审核的条目不会进入
检索"。索引构建器遇到已存在条目时不改状态，见 `app/knowledge/indexer.py`。
"""

from datetime import UTC, datetime
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.db.uow import unit_of_work
from app.knowledge.errors import IllegalTransition, KnowledgeNotFound, OperatorRequired, ReasonRequired
from app.knowledge.status import KnowledgeStatus
from app.models.knowledge import KnowledgeItem
from app.models.knowledge_review import KnowledgeReviewLog
from app.observability import log, metrics
from app.repositories.knowledge import KnowledgeRepository
from app.repositories.knowledge_review import KnowledgeReviewLogRepository

# 各动作的日频次指标名（T2-54 权限与审计："暂停名单最近是否异常频繁变动"的监控依据）。
REVIEW_METRIC = "knowledge_review_action"


class KnowledgeAction(str, Enum):
    """核准流程的动作。`str, Enum` 与项目其它枚举（如 `KnowledgeStatus`）同写法。"""

    SUBMIT = "submit"  # draft -> draft（提交待审；draft 即"待审"，不引入第五个状态）
    APPROVE = "approve"  # draft -> approved
    REJECT = "reject"  # draft -> draft（打回，带理由）
    SUSPEND = "suspend"  # approved -> suspended（带理由）
    RESUME = "resume"  # suspended -> approved
    DEPRECATE = "deprecate"  # approved/suspended -> deprecated


# 合法迁移表：`当前状态 -> {动作: 目标状态}`。
# `DEPRECATED` 是终态，没有任何出边：条目废弃是永久的，若要恢复它应新建条目而不是改回。
ALLOWED_TRANSITIONS: dict[KnowledgeStatus, dict[KnowledgeAction, KnowledgeStatus]] = {
    KnowledgeStatus.DRAFT: {
        KnowledgeAction.APPROVE: KnowledgeStatus.APPROVED,
        KnowledgeAction.SUBMIT: KnowledgeStatus.DRAFT,
        # 打回：`draft` 即"待审"，打回后仍回到"待审"，必须带理由。
        KnowledgeAction.REJECT: KnowledgeStatus.DRAFT,
    },
    KnowledgeStatus.APPROVED: {
        KnowledgeAction.SUSPEND: KnowledgeStatus.SUSPENDED,
        KnowledgeAction.DEPRECATE: KnowledgeStatus.DEPRECATED,
    },
    KnowledgeStatus.SUSPENDED: {
        KnowledgeAction.RESUME: KnowledgeStatus.APPROVED,
        KnowledgeAction.DEPRECATE: KnowledgeStatus.DEPRECATED,
    },
    KnowledgeStatus.DEPRECATED: {},
}

_REASON_REQUIRED_ACTIONS = frozenset({KnowledgeAction.SUSPEND, KnowledgeAction.REJECT, KnowledgeAction.DEPRECATE})


class KnowledgeReviewService:
    """知识条目状态机与核准流程。所有变更留痕。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = KnowledgeRepository(session)
        self._log_repo = KnowledgeReviewLogRepository(session)

    async def apply(
        self,
        *,
        knowledge_id: str,
        action: KnowledgeAction,
        operator_id: str,
        reason: str | None = None,
        consulted_ref: str | None = None,
    ) -> KnowledgeItem:
        """执行一次状态迁移。非法迁移与缺理由均抛异常。

        `operator_id` 必填：核准入口不接受"无操作人"的调用，保证每条变更都能追到人。
        `consulted_ref` 可选：用于"因某次问诊触发的暂停"这类场景，记录关联的问诊 id。
        """
        if not operator_id or not operator_id.strip():
            raise OperatorRequired()

        item = await self._repo.get_by_id(knowledge_id)
        if item is None:
            raise KnowledgeNotFound(knowledge_id)

        current = KnowledgeStatus(item.status)
        target = ALLOWED_TRANSITIONS[current].get(action)
        if target is None:
            raise IllegalTransition(f"{current.value} 不允许动作 {action.value}")
        self._require_reason(action, reason)

        async with unit_of_work(self._session, name="knowledge.review"):
            await self._write_log(item, current, target, action, operator_id, reason, consulted_ref)
            await self._repo.update_status(knowledge_id, target, reason)

        # 指标上报放在事务提交之后：回滚的变更不应计入"今天暂停了几次"，否则监控口径
        # 会把失败尝试算成真实变更。
        metrics.incr(REVIEW_METRIC, tags={"action": action.value})
        log.info(
            "知识条目状态迁移",
            knowledge_id=knowledge_id,
            action=action.value,
            from_status=current.value,
            to_status=target.value,
            operator_id=operator_id,
        )
        updated = await self._repo.get_by_id(knowledge_id)
        # 迁移后条目必然存在（刚在同一事务内更新过），此断言只为收口类型且暴露意外删除。
        assert updated is not None
        return updated

    def _require_reason(self, action: KnowledgeAction, reason: str | None) -> None:
        """暂停、打回、废弃必须给出理由；核准与恢复可不填。"""
        if action in _REASON_REQUIRED_ACTIONS and (not reason or not reason.strip()):
            raise ReasonRequired(f"动作 {action.value} 必须给出理由")

    async def _write_log(
        self,
        item: KnowledgeItem,
        current: KnowledgeStatus,
        target: KnowledgeStatus,
        action: KnowledgeAction,
        operator_id: str,
        reason: str | None,
        consulted_ref: str | None,
    ) -> None:
        """写一条变更记录。与主表更新在同一事务内，保证"有变更必有日志"。"""
        normalized = reason.strip() if reason and reason.strip() else None
        await self._log_repo.insert(
            KnowledgeReviewLog(
                id=new_id("krl"),
                knowledge_id=item.knowledge_id,
                from_status=current.value,
                to_status=target.value,
                action=action.value,
                reason=normalized,
                operator_id=operator_id,
                consulted_ref=consulted_ref,
                created_at=datetime.now(UTC),
            )
        )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "REVIEW_METRIC",
    "KnowledgeAction",
    "KnowledgeReviewService",
]
