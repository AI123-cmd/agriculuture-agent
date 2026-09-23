"""AI 对话编排服务：喂入 LangGraph 诊断图，落库并返回纯文本。"""
from sqlalchemy.orm import Session

from app.models import AIConversation, AIMessage, Crop, FarmerProfile, UserProfileTag
from app.schemas import ChatRequest
from app.services.memory_service import MemoryService
from app.services.profile_service import ProfileService


class AIService:
    async def chat(self, db: Session, user_id: str, req: ChatRequest) -> dict:
        context = await self.build_context(db, user_id, req.cropTag)

        from app.agent import get_agent  # 延迟导入，避免与 services 包形成循环依赖

        graph = await get_agent()
        initial_state = {
            "session_id": req.sessionId,
            "user_id": user_id,
            "crop_tag": req.cropTag,
            "image_url": req.imageUrl,
            "user_message": req.message,
            "context": context,
            "messages": [{"role": "user", "content": req.message}],
        }
        config = {"configurable": {"thread_id": req.sessionId, "db": db}}
        result = await graph.ainvoke(initial_state, config=config)

        reply = result.get("reply") or ""
        related = result.get("related_suggestions") or ["追肥建议", "排水建议", "3天后复查"]

        self._persist(db, user_id, req, reply)

        tags_added = [req.cropTag] if req.cropTag else []
        return {
            "sessionId": req.sessionId,
            "reply": reply,
            "relatedSuggestions": related,
            "tagsAdded": tags_added,
        }

    def _persist(self, db: Session, user_id: str, req: ChatRequest, reply: str) -> None:
        conv = db.query(AIConversation).filter(AIConversation.id == req.sessionId).first()
        if conv is None:
            conv = AIConversation(id=req.sessionId, user_id=user_id, title=req.message[:15], crop_tag=req.cropTag)
            db.add(conv)
        else:
            conv.crop_tag = req.cropTag or conv.crop_tag

        db.add(AIMessage(conversation_id=req.sessionId, role="user", content=req.message, image_url=req.imageUrl))
        db.add(AIMessage(conversation_id=req.sessionId, role="ai", content=reply))

        tags_added = [req.cropTag] if req.cropTag else []
        ProfileService().add_tags(db, user_id, tags_added)
        MemoryService().remember(db, user_id, req.sessionId, req.message, reply)

        db.commit()

    async def build_context(self, db: Session, user_id: str, crop_tag: str | None) -> dict:
        profile = db.query(FarmerProfile).filter(FarmerProfile.user_id == user_id).first()
        crops = db.query(Crop).filter(Crop.user_id == user_id).all()
        crop = next((c for c in crops if c.crop_name == crop_tag), crops[0] if crops else None)
        tag = db.query(UserProfileTag).filter(UserProfileTag.user_id == user_id).first()

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

    def history(self, db: Session, user_id: str, page: int = 1, size: int = 20) -> dict:
        q = db.query(AIConversation).filter(AIConversation.user_id == user_id).order_by(AIConversation.update_time.desc())
        total = q.count()
        convs = q.offset((page - 1) * size).limit(size).all()

        result = []
        for conv in convs:
            msgs = (
                db.query(AIMessage)
                .filter(AIMessage.conversation_id == conv.id)
                .order_by(AIMessage.id)
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

    def delete_history(self, db: Session, user_id: str, session_id: str) -> None:
        conv = db.query(AIConversation).filter(AIConversation.id == session_id, AIConversation.user_id == user_id).first()
        if conv:
            db.query(AIMessage).filter(AIMessage.conversation_id == session_id).delete(synchronize_session=False)
            db.delete(conv)
            db.commit()