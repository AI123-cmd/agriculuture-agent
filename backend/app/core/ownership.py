"""归属安全异常：把"忘记传归属参数"与"归属不符"在日志里区分开。

`OwnershipError` 取 code 403，`main.py` 的 `app_exception_handler` 用 `exc.code`
同时当业务码与 HTTP 状态码，故无需改动 handler 即可得到 403 响应。
"""

from app.core.exceptions import AppException


class OwnershipError(AppException):
    """归属不符。对"不存在"与"不属于你"返回同一错误，避免用差异枚举他人 ID。"""

    def __init__(self, message: str = "资源不存在或无权访问"):
        super().__init__(403, message)


class MissingOwnerError(OwnershipError):
    """编程错误：仓储调用未传 user_id，与业务 403 在日志中可区分。"""

    def __init__(self, message: str = "缺少归属参数，拒绝执行查询"):
        super().__init__(message)
