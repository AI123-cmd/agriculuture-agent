"""记忆服务：会话摘要与关键事实读写。

两个方法都保持"不提交"约定（原实现也是），事务边界属于调用方的业务用例。
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import memory_repo, tag_repo


class MemoryService:
    async def recall(self, db: AsyncSession, user_id: str, query: str) -> dict:
        """回答前组装中期（摘要）+ 长期（事实/画像）记忆上下文。"""
        tag = await tag_repo.get_for_user(db, user_id)
        summaries = await memory_repo.list_by_update_desc(db, user_id)

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

    async def remember(self, db: AsyncSession, user_id: str, session_id: str, user_message: str, ai_reply: str) -> None:
        """回答后记录摘要与关键事实（不提交），同一会话复用一条记录。"""
        summary = (ai_reply or "")[:200]
        await memory_repo.upsert(db, user_id, session_id, summary, self._extract_facts(user_message))

    @staticmethod
    def _extract_facts(text: str) -> list[str]:
        """规则化从用户输入抽取关键事实（可替换为轻量 LLM 抽取）。"""
        if not text:
            return []
        segments = re.split(r"[，。；！？\n,;!?]+", text)
        facts = [s.strip() for s in segments if len(s.strip()) >= 4]
        return facts[:5]


__all__ = ["MemoryService"]
