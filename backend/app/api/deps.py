"""FastAPI 依赖注入：从 Authorization header 解析 token 并提取当前用户。"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import Unauthorized
from app.core.security import get_user_id

_bearer = HTTPBearer(auto_error=False)


def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    """校验 Bearer token，返回当前用户 id；无效则抛 401。"""
    if cred is None or not cred.credentials:
        raise Unauthorized()
    user_id = get_user_id(cred.credentials)
    if user_id is None:
        raise Unauthorized()
    return user_id
