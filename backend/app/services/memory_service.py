"""记忆服务：会话摘要与关键事实读写。"""
import re

from sqlalchemy.orm import Session

from app.db.base import new_id
from app.models import MemorySummary, UserProfileTag


class MemoryService:
    def recall(self, db: Session, user_id: str, query: str) -> dict:
        """回答前组装中期（摘要）+ 长期（事实/画像）记忆上下文。"""
        tag = db.query(UserProfileTag).filter(UserProfileTag.user_id == user_id).first()
        summaries = (
            db.query(MemorySummary)
            .filter(MemorySummary.user_id == user_id)
            .order_by(MemorySummary.update_time.desc())
            .all()
        )

        session_summary = summaries[0].summary if summaries else ""
        facts: list[str] = []
        for row in summaries:
            for fact in row.key_facts or []:
                if fact and fact not in facts:
                    facts.append(fact)

        return {
            "profile_tags": tag.tags if tag and tag.tags else [],
            "session_summary": session_summary,
            "relevant_facts": facts[-5:],
        }

    def remember(self, db: Session, user_id: str, session_id: str, user_message: str, ai_reply: str) -> None:
        """回答后记录摘要与关键事实（不提交），同一会话复用一条记录。"""
        facts = self._extract_facts(user_message)
        summary = (ai_reply or "")[:200]

        existing = (
            db.query(MemorySummary)
            .filter(MemorySummary.user_id == user_id, MemorySummary.session_id == session_id)
            .first()
        )
        if existing:
            existing.summary = summary
            existing.key_facts = list(set((existing.key_facts or []) + facts))
            return

        db.add(
            MemorySummary(
                id=new_id("mem"),
                user_id=user_id,
                session_id=session_id,
                summary=summary,
                key_facts=facts,
            )
        )

    @staticmethod
    def _extract_facts(text: str) -> list[str]:
        """规则化从用户输入抽取关键事实（可替换为轻量 LLM 抽取）。"""
        if not text:
            return []
        segments = re.split(r"[，。；！？\n,;!?]+", text)
        facts = [s.strip() for s in segments if len(s.strip()) >= 4]
        return facts[:5]