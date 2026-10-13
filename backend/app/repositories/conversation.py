"""会话与作物仓储。

`ai_message` 的仓储在 `app/repositories/message.py`：它没有 `user_id` 列，
归属只能经父会话 `conversation_id` 间接判定，形态与 `OwnedRepository` 不同。
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import OwnershipError
from app.db.base import new_id
from app.models import AIConversation, AIMessage
from app.models.crop import Crop
from app.repositories.base import OwnedRepository, require_user_id
from app.repositories.message import message_repo


class ConversationRepository(OwnedRepository[AIConversation]):
    def __init__(self) -> None:
        super().__init__(AIConversation)

    async def resolve_session_id(self, db: AsyncSession, user_id: str, requested: str | None) -> str:
        """决定本次对话实际使用的主键。

        三种情形：请求的 `sessionId` 是自己已有的会话 → 沿用；该 id 尚未被占用
        → 用它新建（前端换手机或清缓存后会重复提交同一个本地 id，属正常路径）；
        该 id 已被他人占用 → 换一个新主键新建，绝不接管、绝不覆盖他人数据。

        第三种的判定用 `id_taken`（见其 docstring 说明为何允许这一处跨用户探测）。
        不抛 403：产品说明书 4.2 里 `sessionId` 由前端生成，农户换设备后必然重复，
        报错会让正常用户卡死。
        """
        uid = require_user_id(self.model.__name__, user_id)
        if not requested:
            return new_id("sess")
        if await self.get(db, uid, requested) is not None:
            return requested
        if await self.id_taken(db, requested):
            return new_id("sess")
        return requested

    async def id_taken(self, db: AsyncSession, conv_id: str) -> bool:
        """主键是否已被占用（不区分归属）。

        这是"基类不提供逃生舱"的一处**显式例外**，理由与边界都写在这里：
        唯一用途是挑一个不冲突的新主键，故只回答"能否用这个 id 建新会话"，
        既不返回行内容，也不向调用方暴露归属。若不这样做，A 用 B 的 sessionId
        发消息时只能靠主键冲突报错，而那会把"该 id 存在"变成 500。
        """
        stmt = select(AIConversation.id).where(AIConversation.id == conv_id).limit(1)
        return (await db.execute(stmt)).first() is not None

    def preview_stmt(self, user_id: str, *, page: int, size: int) -> Select[Any]:
        """窗口函数版的分页语句（不执行，供 `EXPLAIN` 与单测核对 SQL 形态）。"""
        uid = self._require_user(user_id)
        agg = message_repo.last_contents_by_conversation()
        return (
            select(AIConversation, func.coalesce(agg.c.msg_count, 0), agg.c.content)
            .outerjoin(agg, agg.c.conversation_id == AIConversation.id)
            .where(AIConversation.user_id == uid)
            .order_by(AIConversation.update_time.desc())
            .limit(size)
            .offset((page - 1) * size)
        )

    def preview_lateral_stmt(self, user_id: str, *, page: int, size: int) -> Select[Any]:
        """`LATERAL` 版的分页语句（不执行，供 `EXPLAIN` 与单测核对 SQL 形态）。"""
        uid = self._require_user(user_id)
        # 消息数用侧查询内部的窗口函数算，而不是再套一个相关子查询：
        # 侧查询的 FROM 也是 `ai_message`，SQLAlchemy 的自动关联会把嵌套子查询的
        # `FROM ai_message` 消掉、转而关联到侧查询的 `ai_message` 上，语义就变成
        # `count(*) where conversation_id = m.id`（恒为 0/1）。窗口函数在同一遍里
        # 按分区计数，`LIMIT 1` 之前就已算好，结果就是该会话的消息总数。
        latest = (
            select(
                message_repo.preview_expr().label("content"),
                func.count().over(partition_by=AIMessage.conversation_id).label("msg_count"),
            )
            .where(AIMessage.conversation_id == AIConversation.id)
            .order_by(AIMessage.id.desc())
            .limit(1)
            .lateral("latest")
        )
        return (
            select(AIConversation, func.coalesce(latest.c.msg_count, 0), latest.c.content)
            .outerjoin(latest, true())
            .where(AIConversation.user_id == uid)
            .order_by(AIConversation.update_time.desc())
            .limit(size)
            .offset((page - 1) * size)
        )

    async def paginate_with_preview(
        self, db: AsyncSession, user_id: str, *, page: int, size: int
    ) -> list[tuple[AIConversation, int, str | None]]:
        """一次查询取会话 + 消息数 + 最后一条消息的预览（窗口函数版）。

        改前是 N+1：每条会话单独查一次消息、把整段对话读进内存只为取 30 个字符。
        这里把"每条会话的最后一条消息 + 消息数"做成一个子查询，与分页合进同一条
        SQL，整个历史列表固定 1 次查询（`count` 另计）。

        `LEFT JOIN` 不能改成 `JOIN`：没有消息的会话仍要出现在列表里
        （`messageCount=0`、`preview=""`），内连接会静默丢行。
        """
        result = await db.execute(self.preview_stmt(user_id, page=page, size=size))
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def paginate_with_preview_lateral(
        self, db: AsyncSession, user_id: str, *, page: int, size: int
    ) -> list[tuple[AIConversation, int, str | None]]:
        """`LATERAL` 版本：只对被翻页选中的会话各取一次，不扫全表。

        与窗口函数版二选一，由 `Settings.HISTORY_QUERY_STRATEGY` 决定。两者的差别
        不在写法而在**扫描范围**：窗口函数版的子查询没有 `conversation_id` 上的
        过滤条件，只能整表参与分区（全表扫描 + WindowAgg），多租户下会扫到所有用户
        的消息；`LATERAL` 版带 `WHERE m.conversation_id = c.id`，可走
        `ix_ai_message_conversation_id`，代价与页大小成正比、与表大小无关。
        故消息表变大后应切到 `lateral`，切换只改配置、不动调用方。

        外层用 `outerjoin(latest, true())` 表达 `LEFT JOIN LATERAL ... ON true`。
        """
        result = await db.execute(self.preview_lateral_stmt(user_id, page=page, size=size))
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def delete_with_messages(self, db: AsyncSession, user_id: str, conv_id: str) -> int:
        """先校验归属，再删子表，最后删父表。

        固定这个顺序，是因为现状 `ai_service.delete_history` 只按 `conversation_id`
        删消息、不校验归属；一旦"删了消息但删会话失败"，消息就成了孤儿。
        两处删除都在调用方的同一事务内，由 T1-09 保证回滚。
        """
        if await self.get(db, user_id, conv_id) is None:
            raise OwnershipError("会话不存在或无权访问")
        await message_repo.delete_by_conversation(db, user_id, conv_id)
        return await self.delete(db, user_id, conv_id)

    async def touch_crop_tag(self, db: AsyncSession, user_id: str, conv_id: str, crop_tag: str | None) -> int:
        """仅在传入非空 crop_tag 时更新，且按归属过滤。"""
        if not crop_tag:
            return 0
        return await self.update_fields(db, user_id, conv_id, crop_tag=crop_tag)


class CropRepository(OwnedRepository[Crop]):
    """作物仓储（`farmer_service.save_profile` 的覆盖式重建用它）。"""

    def __init__(self) -> None:
        super().__init__(Crop)

    async def list_for_profile(self, db: AsyncSession, user_id: str) -> Sequence[Crop]:
        """按"主作物优先、其次按创建时间"返回，供档案与上下文组装。"""
        return await self.list_all(db, user_id, order_by=(Crop.is_main.desc(), Crop.create_time))

    async def main_crop(self, db: AsyncSession, user_id: str, crop_tag: str | None = None) -> Crop | None:
        """取上下文用的代表作物：优先命中 `crop_tag`，否则取主作物。

        排序与截断都在 SQL 侧完成（现状是把全部作物取回、在 Python 里 `next(...)`），
        少一次全量物化。
        """
        if crop_tag:
            hit = await self.first(db, user_id, crop_name=crop_tag)
            if hit is not None:
                return hit
        return await self.first(db, user_id, is_main=1)

    async def replace_all(self, db: AsyncSession, user_id: str, items: Sequence[dict]) -> None:
        """覆盖式重建：先按归属清空，再逐条写入，首条为主作物。

        清空与写入必须在调用方的同一事务内完成（T1-09），否则"约束失败导致
        作物列表被清空"会真实发生。`items` 的键用 `crop_name`/`crop_variety`/
        `plant_mu`/`sowing_date`/`growth_stage`，与请求模型的驼峰命名解耦（转换放在服务层）。
        `sowing_date` 是农事历推算的起始时间，缺它则农事历不产出（见 `broadcast/inputs/calendar.py`）。
        """
        uid = require_user_id(self.model.__name__, user_id)
        await self.delete_all(db, uid)
        for index, item in enumerate(items):
            crop = Crop(
                id=new_id("crop"),
                crop_name=item["crop_name"],
                crop_variety=item.get("crop_variety") or None,
                plant_mu=item.get("plant_mu") or 0,
                sowing_date=item.get("sowing_date"),
                growth_stage=item.get("growth_stage") or "",
                is_main=1 if index == 0 else 0,
            )
            await self.add(db, uid, crop)


conversation_repo = ConversationRepository()
crop_repo = CropRepository()

__all__ = ["ConversationRepository", "CropRepository", "conversation_repo", "crop_repo"]
