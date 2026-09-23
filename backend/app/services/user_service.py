"""用户服务：头像上传、注销账号。"""
from sqlalchemy.orm import Session

from app.integrations.storage import get_storage
from app.models import (
    AIConversation,
    AIMessage,
    BroadcastLog,
    Crop,
    Device,
    FarmerProfile,
    Field,
    Location,
    MemorySummary,
    User,
    UserProfileTag,
)


class UserService:
    def upload_avatar(self, db: Session, user_id: str, file) -> str:
        data = file.file.read()
        url = get_storage().save(file.filename or "avatar", data)

        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.avatar_url = url
        db.commit()
        return url

    def cancel_account(self, db: Session, user_id: str) -> None:
        # 先删会话消息（依赖会话 id）
        conv_ids = [c.id for c in db.query(AIConversation).filter(AIConversation.user_id == user_id).all()]
        if conv_ids:
            db.query(AIMessage).filter(AIMessage.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        db.query(AIConversation).filter(AIConversation.user_id == user_id).delete(synchronize_session=False)
        db.query(MemorySummary).filter(MemorySummary.user_id == user_id).delete(synchronize_session=False)
        db.query(BroadcastLog).filter(BroadcastLog.user_id == user_id).delete(synchronize_session=False)
        db.query(Location).filter(Location.user_id == user_id).delete(synchronize_session=False)
        db.query(Device).filter(Device.user_id == user_id).delete(synchronize_session=False)
        db.query(Crop).filter(Crop.user_id == user_id).delete(synchronize_session=False)
        db.query(Field).filter(Field.user_id == user_id).delete(synchronize_session=False)
        db.query(FarmerProfile).filter(FarmerProfile.user_id == user_id).delete(synchronize_session=False)
        db.query(UserProfileTag).filter(UserProfileTag.user_id == user_id).delete(synchronize_session=False)
        db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
        db.commit()