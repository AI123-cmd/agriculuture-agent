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


class TokenRevokedError(Unauthorized):
    """令牌已进吊销名单（该用户已登出）。

    与 `Unauthorized` 同为 401，但单独成类是为了在日志与埋点里把"已登出"与
    "过期/伪造"区分开——前者是正常行为，后者可能是攻击信号。
    """

    def __init__(self, message: str = "登录状态已失效，请重新登录"):
        super().__init__(message)


class ConfigError(AppException):
    """关键配置缺失或不合规。

    取 500 而非 400：这是服务端自身的部署问题，不是调用方能修正的请求错误。
    生产环境缺微信密钥时由登录链路抛出（产品说明书 9.2 第 3 条）。
    """

    def __init__(self, message: str = "服务配置缺失，请联系管理员"):
        super().__init__(500, message)


class NotFound(AppException):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(404, message)


class ServerError(AppException):
    def __init__(self, message: str = "服务器内部错误"):
        super().__init__(500, message)
