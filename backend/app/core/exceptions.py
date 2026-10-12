"""业务异常定义。"""

from typing import Any


class AppException(Exception):
    """业务异常，携带错误码与消息，由全局 handler 统一转换为响应。

    `detail` 是可选的附加定位信息（如校验失败的具体字段与失败层级），由 handler 放进响应的
    `data` 字段。单列一个字段而不是把结构化信息拼进 `message`：`message` 是给农户看的中文文案，
    `detail` 是给前端定位输入框用的机器可读结构，两者受众不同，混在一起会让文案也被迫机器化。
    """

    def __init__(self, code: int = 400, message: str = "请求参数错误", detail: Any | None = None):
        self.code = code
        self.message = message
        self.detail = detail
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


class Forbidden(AppException):
    """已认证但无权访问该接口。

    取 403 而非 401：401 表示"身份未确认"（前端应重新登录），403 表示"身份已知但不被
    允许"（前端重新登录也没用）。知识条目核准接口只对后台运营账号开放（T2-54），农户
    令牌验签通过但不具备该权限，故用 403，避免前端误触发重新登录。
    """

    def __init__(self, message: str = "无权访问该接口"):
        super().__init__(403, message)


class BadRequest(AppException):
    """请求参数或上传内容不合规。

    `code` 取 400：责任在调用方，改请求即可修正。上传校验失败走这一类，
    使"图片不合法"与"服务端故障"在日志里可区分。
    """

    def __init__(self, message: str = "请求参数错误"):
        super().__init__(400, message)


class InvalidStateError(AppException):
    """对象当前状态不允许该操作，属请求错误（取 400）。

    与 `BadRequest` 同为 400，但单独成类是为了让"状态机违约"与"参数格式错误"在日志与
    埋点里可区分：前者的请求本身格式合法，只是打在了一个不该被打的对象上。T3-15 的
    "被抑制的提示条目不接受执行勾选"是首个用例——该条目从未展示给农户，农户不可能勾选
    它，请求合法但状态不允许。
    """

    def __init__(self, message: str = "当前状态不允许该操作"):
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


class ValidationError(AppException):
    """业务校验失败，带结构化的字段定位信息（取 400）。

    与 `BadRequest` 的区别在 `detail`：`BadRequest` 表达"请求整体不合法"，本类表达"某一份业务
    数据的某个字段不满足规则"。三类接入的字段映射校验（T5-05 的字段级/映射级/换算级）是主要
    用例——前端要据 `detail={"rawField": ..., "layer": ...}` 把错误挂到具体输入框，而不是弹一个
    笼统的失败提示。取 400：责任在调用方提交的数据，改数据即可通过。
    """

    def __init__(self, message: str = "数据校验未通过", detail: Any | None = None):
        super().__init__(400, message, detail)


class DeviceAuthError(AppException):
    """设备密钥校验失败（取 403）。

    取 403 而非 401：这个入口的凭证是绑定下发的设备密钥，不是登录态，"重新登录"对它没有意义。
    更重要的是设备固件的重试策略——多数 4G DTU 只对 5xx 重试，403 会被当作"凭证无效"而停止重试，
    避免用错误密钥反复冲击上报入口（T5-06 的设计要点）。SN 不存在与密钥错误返回同一错误，
    使攻击者无法靠响应差异枚举合法 SN。
    """

    def __init__(self, message: str = "设备鉴权失败"):
        super().__init__(403, message)
