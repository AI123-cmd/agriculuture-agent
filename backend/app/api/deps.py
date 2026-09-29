"""FastAPI 依赖注入：从 Authorization header 解析令牌并提取当前用户。"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Forbidden, Unauthorized
from app.db.session import get_db
from app.services import get_auth_service

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """校验 Bearer 令牌，返回当前用户 id；无效则抛 401。

    走 `AuthService.verify` 而不是直接解码：只有这条链路包含吊销名单检查，
    登出才能让旧令牌立即失效（产品说明书 9.2 第 2 条）。
    """
    if cred is None or not cred.credentials:
        raise Unauthorized()
    user = await get_auth_service().verify(db, cred.credentials)
    return user.id


async def require_review_operator(user_id: str = Depends(get_current_user)) -> str:
    """核准接口的后台权限校验（T2-54）：令牌有效且账号在运营白名单内，返回 `operator_id`。

    先 `get_current_user` 确保"已认证"，再做白名单判断——两者语义不同，不可合并：

    - 未登录/令牌失效 → 401（`get_current_user` 抛出），前端应重新登录；
    - 已登录但非运营账号（如农户令牌）→ 403，前端重新登录也没用。

    白名单从 `Settings.KNOWLEDGE_REVIEW_OPERATOR_IDS` 读取（逗号分隔），空串 = 谁都不能调用。
    fail-closed 是刻意的：核准是"人工审核后方可上架"这条红线的执行口，宁可漏配时全部拒绝，
    也不允许"忘了配置就默认放行"。
    """
    allowed = {item.strip() for item in get_settings().KNOWLEDGE_REVIEW_OPERATOR_IDS.split(",") if item.strip()}
    if user_id not in allowed:
        raise Forbidden("该接口仅对后台运营账号开放")
    return user_id


__all__ = ["get_current_user", "require_review_operator"]
