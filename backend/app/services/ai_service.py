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

from app.db.uow import unit_of_work
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

        # 图执行（秒级）刻意放在事务之外：把它包进事务会长时间持锁，且检查点写入
        # 由 AsyncPostgresSaver 自己的连接池管理，与业务事务本就不是同一连接
        # （技术方案 7.5 的连接池 autocommit=True 也印证这点）。一致性靠顺序保证：
        # 业务事务成功后才有业务记录，检查点先行不会造成业务数据的部分写入。
        async with unit_of_work(db, name="ai.chat"):
            await self._persist(db, user_id, session_id, req, reply)

        tags_added = [req.cropTag] if req.cropTag else []
        return {
            "sessionId": session_id,
            "reply": reply,
            "relatedSuggestions": related,
            "tagsAdded": tags_added,
        }

    async def _persist(self, db: AsyncSession, user_id: str, session_id: str, req: ChatRequest, reply: str) -> None:
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

        await message_repo.add_many(
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
        total = await conversation_repo.count(db, user_id)
        convs = await conversation_repo.list_for_history(db, user_id, page=page, size=size)

        result = []
        for conv in convs:
            msgs = await message_repo.list_by_conversation(db, user_id, conv.id)
            messages = [{"id": m.id, "role": m.role, "content": m.content} for m in msgs]
            preview = msgs[-1].content[:30] if msgs else ""
            result.append(
                {
                    "id": conv.id,
                    "title": conv.title or "新对话",
                    "preview": preview,
                    "updatedAt": int(conv.update_time.timestamp() * 1000) if conv.update_time else 0,
                    "messages": messages,
                }
            )
        return {"list": result, "total": total}

    async def delete_history(self, db: AsyncSession, user_id: str, session_id: str) -> None:
        """删除会话及其消息：归属校验在仓储内完成，不属于当前用户抛 403。

        原实现先按 `id + user_id` 查会话、只在命中时删消息，看起来安全；但子表删除
        只按 `conversation_id`，一旦调用方漏判就成孤儿。现在两步都带归属条件，
        且同处一个事务——"删了消息但删会话失败"不再可能。
        """
        async with unit_of_work(db, name="ai.delete_history"):
            await conversation_repo.delete_with_messages(db, user_id, session_id)


__all__ = ["AIService"]
