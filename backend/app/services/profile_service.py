"""用户画像服务：标签更新、对话计数。"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.base import new_id
from app.models import UserProfileTag


class ProfileService:
    def update_tags(self, db: Session, user_id: str, tags: list[str]) -> None:
        """覆盖式更新画像标签（不提交，由调用方 commit）。"""
        tag = db.query(UserProfileTag).filter(UserProfileTag.user_id == user_id).first()
        if tag is None:
            tag = UserProfileTag(id=new_id("tag"), user_id=user_id, tags=[], chat_count=0)
            db.add(tag)
        tag.tags = list(set(tags))

    def add_tags(self, db: Session, user_id: str, new_tags: list[str]) -> None:
        """增量合并标签并累加对话次数（不提交）。"""
        tag = db.query(UserProfileTag).filter(UserProfileTag.user_id == user_id).first()
        if tag is None:
            tag = UserProfileTag(id=new_id("tag"), user_id=user_id, tags=[], chat_count=0)
            db.add(tag)
        merged = list(set((tag.tags or []) + [t for t in new_tags if t]))
        tag.tags = merged
        tag.chat_count = (tag.chat_count or 0) + 1
        tag.last_chat_at = datetime.now()

    def get_tags(self, db: Session, user_id: str) -> list[str]:
        tag = db.query(UserProfileTag).filter(UserProfileTag.user_id == user_id).first()
        return tag.tags if tag and tag.tags else []