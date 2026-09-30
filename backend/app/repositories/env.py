"""环境读数仓储（TimescaleDB 超表）。

`env_reading` 的主键是 `(id, observed_at)` 复合主键，不是 `OwnedRepository` 要求的
单列 `id`，因此本仓储不继承基类，只复用 `require_user_id` 做归属参数校验。查询一律
带 `user_id` 条件，跨农户读取返回空。
"""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EnvReading
from app.repositories.base import require_user_id


class EnvReadingRepository:
    """读数仓储：服务环境事实采集（T3-02）与环境看板（T5-08）。"""

    async def latest_batch(
        self, db: AsyncSession, user_id: str, *, farm_entry_id: str | None = None
    ) -> Sequence[EnvReading]:
        """取该用户最新一个上报批次（同一 `observed_at` 下的全部指标）。

        一次上报的多个指标共享同一 `observed_at`，因此"最新读数"是"最新时刻下的
        一组行"而不是单行。取全局最大 `observed_at` 后再筛同刻行，避免用
        `DISTINCT ON` 逐指标取最新而在跨指标时间戳不齐时得到混合批次。
        """
        uid = require_user_id("EnvReading", user_id)
        conditions = [EnvReading.user_id == uid]
        if farm_entry_id is not None:
            conditions.append(EnvReading.farm_entry_id == farm_entry_id)

        max_at = (await db.execute(select(func.max(EnvReading.observed_at)).where(*conditions))).scalar_one_or_none()
        if max_at is None:
            return []
        stmt = select(EnvReading).where(*conditions, EnvReading.observed_at == max_at)
        return (await db.execute(stmt)).scalars().all()


env_reading_repo = EnvReadingRepository()

__all__ = ["EnvReadingRepository", "env_reading_repo"]
