"""处方卡仓储（T3-15，技术方案 12.4）。

处方卡经 `consultation_id` 归属问诊，T3-09 登记回访时按 `consultation_id` 反查（`consult_repo`
里的 `get_card_by_consultation`）。T3-15 的入台账走的是按**主键** `card.id` 取卡片，故单独
建立本仓储，承载"按 id 取卡 + 回写 ledger_saved"这一对用例，而不把这两件事塞进问诊仓储——
问诊仓储管的是 `consultation`，逐项新增卡片方法会让职责边界模糊。

只用基类的既有组合，不自造裸查询（守全局约定 L4，禁止 `db.query`）：
`get` 直接沿用基类（按 id 取并强制归属），`mark_ledger_saved` 走 `update_fields` 回写布尔列。
"""

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


prescription_repo = PrescriptionRepository()

__all__ = ["PrescriptionRepository", "prescription_repo"]
