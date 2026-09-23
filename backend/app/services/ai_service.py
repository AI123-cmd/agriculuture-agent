"""AI 对话编排服务：喂入 LangGraph 诊断图，落库并返回纯文本。"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AIConversation, AIMessage, Crop, FarmerProfile, UserProfileTag
from app.schemas import ChatRequest
from app.services.memory_service import MemoryService
from app.services.profile_service import ProfileService


class AIService:
    async def chat(self, db: AsyncSession, user_id: str, req: ChatRequest) -> dict:
        context = await self.build_context(db, user_id, req.cropTag)

        from app.agent import get_agent  # 延迟导入，避免与 services 包形成循环依赖

        graph = await get_agent()
        # 状态是异构字典，交给 LangGraph 以 Any 输入处理，避免 mypy 对重载的过度收敛
        initial_state: dict[str, Any] = {
            "session_id": req.sessionId,
            "user_id": user_id,
            "crop_tag": req.cropTag,
            "image_url": req.imageUrl,
            "user_message": req.message,
            "context": context,
            "messages": [{"role": "user", "content": req.message}],
        }
        config: RunnableConfig = {"configurable": {"thread_id": req.sessionId, "db": db}}
        result = await graph.ainvoke(initial_state, config=config)

        reply = result.get("reply") or ""
        related = result.get("related_suggestions") or ["追肥建议", "排水建议", "3天后复查"]

        await self._persist(db, user_id, req, reply)

        tags_added = [req.cropTag] if req.cropTag else []
        return {
            "sessionId": req.sessionId,
            "reply": reply,
            "relatedSuggestions": related,
            "tagsAdded": tags_added,
        }

    async def _persist(self, db: AsyncSession, user_id: str, req: ChatRequest, reply: str) -> None:
        conv = (await db.execute(select(AIConversation).where(AIConversation.id == req.sessionId))).scalars().first()
        if conv is None:
            conv = AIConversation(id=req.sessionId, user_id=user_id, title=req.message[:15], crop_tag=req.cropTag)
            db.add(conv)
        else:
            conv.crop_tag = req.cropTag or conv.crop_tag

        db.add(AIMessage(conversation_id=req.sessionId, role="user", content=req.message, image_url=req.imageUrl))
        db.add(AIMessage(conversation_id=req.sessionId, role="ai", content=reply))

        tags_added = [req.cropTag] if req.cropTag else []
        await ProfileService().add_tags(db, user_id, tags_added)
        await MemoryService().remember(db, user_id, req.sessionId, req.message, reply)

        await db.commit()

    async def build_context(self, db: AsyncSession, user_id: str, crop_tag: str | None) -> dict:
        profile = (await db.execute(select(FarmerProfile).where(FarmerProfile.user_id == user_id))).scalars().first()
        crops = (await db.execute(select(Crop).where(Crop.user_id == user_id))).scalars().all()
        crop = next((c for c in crops if c.crop_name == crop_tag), crops[0] if crops else None)
        tag = (await db.execute(select(UserProfileTag).where(UserProfileTag.user_id == user_id))).scalars().first()

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
        total = (
            await db.execute(select(func.count()).select_from(AIConversation).where(AIConversation.user_id == user_id))
        ).scalar_one()
        convs = (
            (
                await db.execute(
                    select(AIConversation)
                    .where(AIConversation.user_id == user_id)
                    .order_by(AIConversation.update_time.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )

        result = []
        for conv in convs:
            msgs = (
                (await db.execute(select(AIMessage).where(AIMessage.conversation_id == conv.id).order_by(AIMessage.id)))
                .scalars()
                .all()
            )
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
        conv = (
            (
                await db.execute(
                    select(AIConversation).where(AIConversation.id == session_id, AIConversation.user_id == user_id)
                )
            )
            .scalars()
            .first()
        )
        if conv:
            await db.execute(delete(AIMessage).where(AIMessage.conversation_id == session_id))
            await db.delete(conv)
            await db.commit()
