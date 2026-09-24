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


class BadRequest(AppException):
    """请求参数或上传内容不合规。

    `code` 取 400：责任在调用方，改请求即可修正。上传校验失败走这一类，
    使"图片不合法"与"服务端故障"在日志里可区分。
    """

    def __init__(self, message: str = "请求参数错误"):
        super().__init__(400, message)


class ConflictError(AppException):
    """唯一约束冲突或乐观锁失败。

    取 409 而不是 500：这是"重试即可"的并发结果，不是服务端故障。消息刻意不包含
    原始 SQL 与表名——那属于实现细节，泄露给调用方既无用又给了探测表结构的机会。
    """

    def __init__(self, message: str = "数据已被并发修改，请重试"):
        super().__init__(409, message)


class TransactionError(AppException):
    """事务执行失败。

    取 500：事务失败属服务端问题，调用方无法通过改请求修正。单独成类是给后续
    降级判断留出口——T2 阶段可按异常类型分流，而不必解析数据库原始错误。
    """

    def __init__(self, message: str = "数据处理失败，请稍后重试"):
        super().__init__(500, message)


class ServerError(AppException):
    def __init__(self, message: str = "服务器内部错误"):
        super().__init__(500, message)


class EnumParseError(AppException):
    """自由文本落到封闭枚举时解析失败，属契约违约（技术方案 5.2）。

    取 500 而非 400：触发点都在服务端内部——模型输出、YAML 配置、数据库字符串列。
    责任不在调用方，改请求无法修正，因此归入服务端故障一类；调用方（T2-13）拿到
    它之后的正确动作是重试一次再降级，而不是把错误透传给农户。
    """

    def __init__(self, message: str = "枚举取值非法"):
        super().__init__(500, message)
