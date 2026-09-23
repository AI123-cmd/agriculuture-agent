"""认证服务：code 换 openid -> 建/查用户 -> 签发 token。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_token
from app.db.base import new_id
from app.integrations.wechat import code2session
from app.models import User
from app.schemas.user import UserInfoIn


class AuthService:
    async def login(self, db: AsyncSession, code: str, user_info: UserInfoIn | None = None) -> dict:
        session_data = code2session(code)
        openid = session_data["openid"]

        user = (await db.execute(select(User).where(User.openid == openid))).scalars().first()
        is_new_user = user is None

        if user is None:
            user = User(id=new_id("user"), openid=openid)
            db.add(user)
            await db.flush()

        # 更新用户资料
        if user_info:
            if getattr(user_info, "nickName", None):
                user.nick_name = user_info.nickName
            if getattr(user_info, "avatarUrl", None):
                user.avatar_url = user_info.avatarUrl
            if getattr(user_info, "gender", None):
                user.gender = user_info.gender
            if getattr(user_info, "country", None):
                user.country = user_info.country
            if getattr(user_info, "province", None):
                user.province = user_info.province
            if getattr(user_info, "city", None):
                user.city = user_info.city

        token = create_token(user.id)
        await db.commit()
        return {"token": token, "userId": user.id, "isNewUser": is_new_user}
