"""业务异常定义。"""


class AppException(Exception):
    """业务异常，携带错误码与消息，由全局 handler 统一转换为响应。"""

    def __init__(self, code: int = 400, message: str = "请求参数错误"):
        self.code = code
        self.message = message
        super().__init__(message)


class Unauthorized(AppException):
    def __init__(self, message: str = "登录已过期，请重新登录"):
        super().__init__(401, message)


class NotFound(AppException):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(404, message)


class ServerError(AppException):
    def __init__(self, message: str = "服务器内部错误"):
        super().__init__(500, message)