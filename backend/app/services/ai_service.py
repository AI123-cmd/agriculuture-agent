"""AI 对话编排服务：喂入 LangGraph 诊断图，落库并返回纯文本。

本模块是 T1-06 的第一类高危修复点：原 `_persist` 按 `sessionId` 查会话时不带归属，
攻击者枚举到他人 `sessionId` 就能把消息写进他人会话、并改写其 `crop_tag`。
现在会话由 `ConversationRepository.resolve_session_id` 决定：不属于自己的 id
当作新会话处理（前端换设备会重复提交本地 id，报 403 会让正常用户卡死），
但新会话一定用新主键，绝不接管他人数据。
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.uow import unit_of_work
from app.events import EVENT_PRESCRIPTION_CREATED, emit
from app.models import AIConversation, AIMessage
from app.repositories import conversation_repo, crop_repo, message_repo, profile_repo, tag_repo
from app.schemas import ChatRequest
from app.services.memory_service import MemoryService
from app.services.profile_service import ProfileService


class AIService:
    async def chat(self, db: AsyncSession, user_id: str, req: ChatRequest) -> dict:
        # 先定会话主键：归属不符时换新 id，后续持久化与检查点线程都用它
        session_id = await conversation_repo.resolve_session_id(db, user_id, req.sessionId)

        context = await self.build_context(db, user_id, req.cropTag)

        from app.agent import get_agent  # 延迟导入，避免与 services 包形成循环依赖

        graph = await get_agent()
        # 状态是异构字典，交给 LangGraph 以 Any 输入处理，避免 mypy 对重载的过度收敛
        initial_state: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "crop_tag": req.cropTag,
            "image_url": req.imageUrl,
            "user_message": req.message,
            "context": context,
            "messages": [{"role": "user", "content": req.message}],
        }
        config: RunnableConfig = {"configurable": {"thread_id": session_id, "db": db}}
        result = await graph.ainvoke(initial_state, config=config)

        reply = result.get("reply") or ""
        related = result.get("related_suggestions") or ["追肥建议", "排水建议", "3天后复查"]

        # 只有走到 `prescribe` 环节才算产出处方。中途结束的轮次（症状/管理信息
        # 不全，图返回追问文案）不应发 `prescription.created`，否则回访会被登记
        # 在一个并不存在的处方上。延迟导入 `ConsultStage` 与下面的 `get_agent` 同理：
        # `app.agent` 包会连锁导入 services，模块级导入会形成循环。
        from app.agent.state import ConsultStage

        prescribed = result.get("stage") == ConsultStage.PRESCRIBE.value

        # 图执行（秒级）刻意放在事务之外：把它包进事务会长时间持锁，且检查点写入
        # 由 AsyncPostgresSaver 自己的连接池管理，与业务事务本就不是同一连接
        # （技术方案 7.5 的连接池 autocommit=True 也印证这点）。一致性靠顺序保证：
        # 业务事务成功后才有业务记录，检查点先行不会造成业务数据的部分写入。
        async with unit_of_work(db, name="ai.chat"):
            await self._persist(db, user_id, session_id, req, reply, prescribed=prescribed)

        tags_added = [req.cropTag] if req.cropTag else []
        return {
            "sessionId": session_id,
            "reply": reply,
            "relatedSuggestions": related,
            "tagsAdded": tags_added,
        }

    async def _persist(
        self,
        db: AsyncSession,
        user_id: str,
        session_id: str,
        req: ChatRequest,
        reply: str,
        *,
        prescribed: bool,
    ) -> None:
        """落库本轮对话（不提交，事务边界由 `chat` 的 `unit_of_work` 统一收口）。
        `session_id` 由 `chat` 判定归属后传入，这里不再按 id 直查。

        `flush` 不能省：会话工厂是 `autoflush=False`，新建的会话行若未下推，
        `message_repo.add_many` 的归属校验会查不到父会话而误报越权。
        """
        conv = await conversation_repo.get(db, user_id, session_id)
        if conv is None:
            conv = AIConversation(id=session_id, title=req.message[:15], crop_tag=req.cropTag)
            await conversation_repo.add(db, user_id, conv)
            await db.flush()
        else:
            conv.crop_tag = req.cropTag or conv.crop_tag

        messages = await message_repo.add_many(
            db,
            user_id,
            session_id,
            [
                # `ai_message.id` 是 BigInteger 自增，不传字符串主键
                AIMessage(role="user", content=req.message, image_url=req.imageUrl),
                AIMessage(role="ai", content=reply),
            ],
        )

        tags_added = [req.cropTag] if req.cropTag else []
        await ProfileService().add_tags(db, user_id, tags_added)
        await MemoryService().remember(db, user_id, session_id, req.message, reply)

        if prescribed:
            await self._emit_prescription_created(db, user_id, session_id, messages[-1])

    async def _emit_prescription_created(
        self, db: AsyncSession, user_id: str, session_id: str, reply_message: AIMessage
    ) -> None:
        """把"处方已生成"写进发件箱（T1-13）。必须在 `unit_of_work` 内调用。

        事件与业务写在同一事务里，因此不存在"业务提交了但事件没落库"。全流程只
        插一行、不产生额外往返，对话链路的延迟预算不受影响——这正是把回访登记、
        台账写入移出发件箱（异步化）的理由（技术方案 6.6）。

        聚合根取本条 AI 回复消息：处方目前还没有独立表（T3-09 才建），而"一次
        对话产生的这份处方"正是回复消息本身。用它做 `aggregate_id`/幂等键有两点
        好处——天然按轮次唯一，同一会话里多次开方不会撞唯一约束；且它已经是主键，
        消费者可以直接按 id 回查回复原文。

        `flush` 是为了拿到自增主键：`emit` 只 `add` 不 flush，而这里的事件载荷
        与幂等键都用得到 id。这次 flush 本来也会在 `unit_of_work` 提交时发生，
        提前到此处不额外增加往返。
        """
        await db.flush()
        await emit(
            db,
            event_type=EVENT_PRESCRIPTION_CREATED,
            aggregate_type="prescription",
            aggregate_id=str(reply_message.id),
            user_id=user_id,
            payload={
                "session_id": session_id,
                "message_id": reply_message.id,
                "summary": (reply_message.content or "")[:200],
            },
        )

    async def build_context(self, db: AsyncSession, user_id: str, crop_tag: str | None) -> dict:
        """组装诊断上下文。

        四次查询分属不同表，无法合并；且同一个 `AsyncSession` 不是并发安全的，
        因此这里顺序执行、不能改成 `asyncio.gather`。作物查询已在 SQL 侧
        `LIMIT 1`（原实现取全部再在 Python 里挑），少一次全量物化。
        """
        profile = await profile_repo.first(db, user_id)
        crop = await crop_repo.main_crop(db, user_id, crop_tag)
        tag = await tag_repo.first(db, user_id)

        def mask(name: str) -> str:
            return (name[0] + "**") if name else "农户"

        return {
            "farmer": {
                "name": mask(profile.real_name) if profile else "农户",
                "location": f"{profile.province or ''}{profile.city or ''}{profile.district or ''}" if profile else "",
            },
            "crop": {
                "name": crop.crop_name if crop else (crop_tag or "通用农业"),
                "growthStage": crop.growth_stage if crop else "",
                "plantMu": float(crop.plant_mu) if crop else None,
            },
            "profile_tags": tag.tags if tag and tag.tags else [],
        }

    async def history(self, db: AsyncSession, user_id: str, page: int = 1, size: int = 20) -> dict:
        """历史列表：固定 2 次查询（COUNT + 分页带预览）。

        改前是 N+1：先查一页会话，再对每条会话单独查一次消息，最后在 Python 里
        取 `msgs[-1].content[:30]`。`size=20` 时要 22 次查询，且每次把整段对话
        读回内存只为 30 个字符（产品说明书 9.2 第 7 条）。现在预览与消息数都由
        同一条 SQL 的窗口函数带出。

        `messages` 从响应里移除：列表页只需要标题、预览、时间，完整会话走
        `GET /api/v1/ai/consult/{id}`（技术方案 13.1）。把 messages 塞进列表是
        "一次请求返回全部数据"的反模式，既慢又让前端被迫处理大数据包。
        """
        settings = get_settings()
        # 上限保护：前端传 size=1000 时不应真去拉 1000 条会话
        size = max(1, min(size, settings.HISTORY_MAX_PAGE_SIZE))
        total = await conversation_repo.count(db, user_id)

        fetch = (
            conversation_repo.paginate_with_preview_lateral
            if settings.HISTORY_QUERY_STRATEGY == "lateral"
            else conversation_repo.paginate_with_preview
        )
        rows = await fetch(db, user_id, page=max(1, page), size=size)

        return {
            "list": [
                {
                    "id": conv.id,
                    "title": conv.title or "新对话",
                    "preview": preview or "",
                    "messageCount": msg_count,
                    "updatedAt": int(conv.update_time.timestamp() * 1000) if conv.update_time else 0,
                }
                for conv, msg_count, preview in rows
            ],
            "total": total,
        }

    async def delete_history(self, db: AsyncSession, user_id: str, session_id: str) -> None:
        """删除会话及其消息：归属校验在仓储内完成，不属于当前用户抛 403。

        原实现先按 `id + user_id` 查会话、只在命中时删消息，看起来安全；但子表删除
        只按 `conversation_id`，一旦调用方漏判就成孤儿。现在两步都带归属条件，
        且同处一个事务——"删了消息但删会话失败"不再可能。
        """
        async with unit_of_work(db, name="ai.delete_history"):
            await conversation_repo.delete_with_messages(db, user_id, session_id)


__all__ = ["AIService"]
