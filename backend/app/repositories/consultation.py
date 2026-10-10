"""问诊会话仓储（T3-04，技术方案 11.2 / 12.4）。

只提供 T3-04 需要的读取入口 `list_observing`：处于观察期、且尚未登记任何回访任务的
会话。第二条限定是"两个来源互斥"的落点——若一次问诊已有处方卡与回访任务，它应由
到期回访来源（`followup_repo.list_due`）负责，观察期来源必须让位，否则农户同一天会
看到两条内容几乎相同的提示。

写入路径（T2-33 的图驱动问诊落库、T2-19/T2-21 的处方卡落库）不在本项范围，本仓储
只读；后续任务需要写方法时再按 `OwnedRepository` 的既有方法组合，不自造裸查询。

`get_card_by_consultation` 是 T3-09 登记回访任务的反查入口：`prescription.created`
事件的载荷只带 `session_id`（即 `consultation.id`）与 `message_id`，而登记需要拿到
处方卡（`card.followup_plan` / `card.consultation_id` / `card.user_id`）。因此按
`consultation_id` 反查处方卡。放在本仓储而非新建处方卡仓储：处方卡经 `consultation_id`
归属问诊，本仓储已持有 `Consultation` 与 `FollowupTask` 两个模型，再加一个卡片模型不
引入新的依赖方向。
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Consultation, FollowupTask, PrescriptionCard
from app.models.enums import ConsultStatus
from app.repositories.base import OwnedRepository


class ConsultationRepository(OwnedRepository[Consultation]):
    def __init__(self) -> None:
        super().__init__(Consultation)

    async def list_observing(self, db: AsyncSession, user_id: str) -> Sequence[Consultation]:
        """观察期且未登记回访任务的会话。

        用 `NOT EXISTS` 而非先把回访任务的 `consultation_id` 全取回再在 Python 侧过滤：
        过滤放在数据库侧只需一次查询，且"已登记回访"这一子条件与被引用的
        `followup_task.consultation_id` 索引天然对齐。
        """
        uid = self._require_user(user_id)
        registered = select(FollowupTask.id).where(
            FollowupTask.consultation_id == Consultation.id,
            FollowupTask.user_id == uid,
        )
        stmt = (
            self._owned_query(uid)
            .where(Consultation.status == ConsultStatus.OBSERVING.value, ~registered.exists())
            .order_by(Consultation.update_time)
        )
        return (await db.execute(stmt)).scalars().all()

    async def get_card_by_consultation(
        self, db: AsyncSession, user_id: str, consultation_id: str
    ) -> PrescriptionCard | None:
        """按问诊反查处方卡（T3-09 登记回访任务的取值来源），归属不符返回 `None`。

        一次问诊理论上只产出一张处方卡，按 `consultation_id` 取首行即可；仍带归属过滤，
        读不到他人的卡片。`user_id` 由 `prescription.created` 载荷的消费方给出（事件行
        自带 `user_id`），与卡片的 `user_id` 必须一致，否则说明事件与数据不匹配。
        """
        uid = self._require_user(user_id)
        stmt = (
            select(PrescriptionCard)
            .where(PrescriptionCard.user_id == uid, PrescriptionCard.consultation_id == consultation_id)
        )
        return (await db.execute(stmt)).scalars().first()



consult_repo = ConsultationRepository()

__all__ = ["ConsultationRepository", "consult_repo"]
