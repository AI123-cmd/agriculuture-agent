"""认证服务：code 换 openid -> 建/查用户 -> 签发令牌；以及登出与校验。

校验链路（`verify`）是本模块的关键：它把"验签成立"与"仍在有效期内"之外的第三种
失效情形——"用户已主动登出"——纳入判断。少了这一步，`/api/logout` 就只是让前端
删掉本地 token，服务端仍然认这个凭证，直到 7 天自然过期（产品说明书 9.2 第 2 条）。
"""

import hashlib
import logging

from jwt import ExpiredSignatureError, InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConfigError, TokenRevokedError, Unauthorized
from app.core.security import create_token, decode_token, remaining_ttl
from app.core.token_revoker import token_revoker
from app.db.base import new_id
from app.integrations.wechat import code2session
from app.models import User
from app.repositories import user_repo
from app.schemas.user import UserInfoIn

logger = logging.getLogger(__name__)

# mock 登录只用于本地与 CI：openid 由 code 派生，方便测试构造多个互不相同的用户。
# 取完整 code 的 sha256 前 16 位，而不是 code 的前缀——前缀截断会让 "cross-user-a"
# 与 "cross-user-b" 撞成同一个 openid，跨用户用例会因此静默失效。
_MOCK_PREFIX = "mock_openid_"
_MOCK_DIGEST_LEN = 16


class AuthService:
    async def login(self, db: AsyncSession, code: str, user_info: UserInfoIn | None = None) -> dict:
        settings = get_settings()
        if not settings.WECHAT_APP_ID or not settings.WECHAT_APP_SECRET:
            # 生产环境缺密钥必须直接失败，不得降级为模拟登录（9.2 第 3 条）
            if settings.ENV == "production":
                raise ConfigError("生产环境缺少微信密钥，拒绝以模拟登录对外服务")
            session_data = self._mock_login(code)
        else:
            session_data = await code2session(code)

        openid = session_data["openid"]
        user = await user_repo.get_by_openid(db, openid)
        is_new_user = user is None
        if user is None:
            user = User(id=new_id("user"), openid=openid)
            await user_repo.add(db, user)
            # 要拿到 user.id 签令牌，且后续字段赋值都在同一事务内，故先 flush
            await db.flush()

        self._apply_user_info(user, user_info)

        token = create_token(user.id)
        await db.commit()
        return {"token": token, "userId": user.id, "isNewUser": is_new_user}

    async def logout(self, db: AsyncSession, user_id: str, token: str) -> None:
        """登出：把该令牌的 `jti` 写入吊销名单。

        `user_id` 参数看似多余（吊销只需要 token），但它由 `get_current_user` 注入，
        使"未登录调登出"在进入本方法前就返回 401，而不是静默成功。
        """
        payload = decode_token(token)
        await token_revoker.revoke(payload["jti"], ttl=remaining_ttl(token))
        logger.info("用户 %s 登出，令牌 %s 已吊销", user_id, payload["jti"])

    async def verify(self, db: AsyncSession, token: str) -> User:
        """校验令牌并返回用户。验签、过期、吊销、用户存活四道检查缺一不可。"""
        try:
            payload = decode_token(token)
        except ExpiredSignatureError as e:
            raise Unauthorized("登录已过期，请重新登录") from e
        except InvalidTokenError as e:
            # 篡改、错签名、格式非法都落在这里，统一 401 而非 500
            raise Unauthorized("登录凭证无效") from e

        if await token_revoker.is_revoked(payload["jti"]):
            raise TokenRevokedError()

        # 注销账号后其令牌在自然过期前仍是"验签成立"的，查库把这种情况纳入校验
        user = await user_repo.get(db, payload["sub"])
        if user is None:
            raise Unauthorized("用户不存在")
        return user

    def _mock_login(self, code: str) -> dict:
        """非生产环境的模拟登录，openid 由 code 稳定派生。"""
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:_MOCK_DIGEST_LEN]
        return {"openid": f"{_MOCK_PREFIX}{digest}", "mock": True}

    def _apply_user_info(self, user: User, user_info: UserInfoIn | None) -> None:
        """把小程序侧带来的用户资料写入实体。"""
        if not user_info:
            return
        for field, value in (
            ("nick_name", user_info.nickName),
            ("avatar_url", user_info.avatarUrl),
            ("gender", user_info.gender),
            ("country", user_info.country),
            ("province", user_info.province),
            ("city", user_info.city),
        ):
            if value:
                setattr(user, field, value)


__all__ = ["AuthService"]
