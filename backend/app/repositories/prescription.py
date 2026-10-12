"""处方卡仓储（T3-15，技术方案 12.4）。

处方卡经 `consultation_id` 归属问诊，T3-09 登记回访时按 `consultation_id` 反查（`consult_repo`
里的 `get_card_by_consultation`）。T3-15 的入台账走的是按**主键** `card.id` 取卡片，故单独
建立本仓储，承载"按 id 取卡 + 回写 ledger_saved"这一对用例，而不把这两件事塞进问诊仓储——
问诊仓储管的是 `consultation`，逐项新增卡片方法会让职责边界模糊。

只用基类的既有组合，不自造裸查询（守全局约定 L4，禁止 `db.query`）：
`get` 直接沿用基类（按 id 取并强制归属），`mark_ledger_saved` 走 `update_fields` 回写布尔列。

`pick_for_quality_sample` 是一个**显式孤立的无归属查询**（T6-09 线上抽检）：抽检是全平台口径，
横跨全部农户按流程分层取卡，无法按 `user_id` 过滤——与 `ReachRepository.count_by_status`
同属"指标/采样类跨农户读"，故不经 `OwnedRepository` 的归属路径，直接 `select()`（仍在
`app/repositories/` 内，L4 合法）。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PrescriptionCard
from app.repositories.base import OwnedRepository


class PrescriptionRepository(OwnedRepository[PrescriptionCard]):
    """处方卡仓储。缺 `user_id` 抛 `MissingOwnerError`，归属不符读到 `None` / 影响 0 行。"""

    def __init__(self) -> None:
        super().__init__(PrescriptionCard)

    async def mark_ledger_saved(self, db: AsyncSession, user_id: str, card_id: str) -> int:
        """处置方卡「已存入台账」标记，返回受影响行数（0 表示不存在或不属于该农户）。

        置位而非在服务层读改写：入台账的幂等判定与写入已由 `ledger_entry` 的唯一约束兜底，
        本列只服务前端按钮的"已存入台账"状态，一次 UPDATE 即可，无需把卡片对象读出来再写回。
        归属过滤由基类 `update_fields` 施加，他人卡片影响 0 行。
        """
        return await self.update_fields(db, user_id, card_id, ledger_saved=True)

    async def pick_for_quality_sample(
        self,
        db: AsyncSession,
        process_id: str,
        day: date,
        *,
        limit: int,
        exclude_ids: frozenset[str] | set[str] = frozenset(),
    ) -> Sequence[PrescriptionCard]:
        """按流程取某日已开方的处方卡作为抽检候选（全局口径，跨农户）。

        抽检样本来自 `prescription_card`（已开方）而非全部问诊：抽检目标是"模型改写的质量"，
        没有处方的问题不涉及改写（14.5）。按 `process_id` 分层取，使低频流程（`rapeseed`/
        `pig`）也能被抽到——全局随机会让它们长期缺席。`exclude_ids` 排除当天已入样的卡，
        与 `eval_run` 的部分唯一索引一起构成幂等（先查后插，不依赖撞库）。

        按 `created_at` 升序取前 `limit` 张：同日样本的时间分布无所谓，稳定顺序让重复调用
        结果可复现（幂等的一个可观测性质）。
        """
        start = datetime.combine(day, time.min, tzinfo=UTC)
        end = datetime.combine(day, time.max, tzinfo=UTC)
        stmt = select(PrescriptionCard).where(
            PrescriptionCard.process_id == process_id,
            PrescriptionCard.created_at.between(start, end),
        )
        if exclude_ids:
            stmt = stmt.where(PrescriptionCard.id.not_in(exclude_ids))
        stmt = stmt.order_by(PrescriptionCard.created_at, PrescriptionCard.id).limit(limit)
        return (await db.execute(stmt)).scalars().all()


prescription_repo = PrescriptionRepository()

__all__ = ["PrescriptionRepository", "prescription_repo"]
