"""流程配置的加载期异常。

两类错误分开表达：`ConfigLoadError` 是"这份配置读不进来"（文件缺失、YAML 语法错、
模型校验不过），`ConfigIntegrityError` 是"读进来了但自相矛盾"（红线为空、槽位 key
重复、必含小节不在段落顺序内）。分成两个类是为了让启动自检的输出可读——前者要去看
YAML 缩进，后者要去看字段之间的关系。

`ConfigIntegrityError` 继承 `ConfigLoadError`：对调用方而言二者都是"该流程不可用"，
`load_all_configs` 因此只需捕获一个基类就能把坏配置的流程整体跳过，而不会因为漏捕
一种异常让整个服务起不来。
"""


class ConfigLoadError(Exception):
    """流程配置无法加载：文件缺失、YAML 语法错误或字段校验失败。

    刻意不继承 `AppException`：这是启动期/部署期的错误，不是请求期的业务异常，
    不该被 API 的全局 handler 转成 HTTP 响应，而应让进程启动失败或让该流程
    在路由表中被标记不可用。
    """


class ConfigIntegrityError(ConfigLoadError):
    """流程配置的跨字段一致性校验失败。

    同时携带 `process_id` 与 `problems` 列表，因为启动自检要一次性汇报所有问题，
    而不是修一条重启一次。`problems` 逐条可读，便于直接贴进发布检查报告。
    """

    def __init__(self, process_id: str, problems: list[str]) -> None:
        self.process_id = process_id
        self.problems = list(problems)
        detail = "；".join(self.problems)
        super().__init__(f"流程配置一致性校验失败 [{process_id}]: {detail}")


class ConfigUnavailableError(ConfigLoadError):
    """该流程当前无可用配置，路由必须指向转人工。

    与 `ConfigLoadError` 的区别在于时机：后者是「读的时候失败了」，本类是「注册表里
    没有可用版本」。运行期的调用方（智能体、路由）拿到本类时不该重试读取——重试也
    不会有结果，正确反应是把这一条流程判为不可用并转人工（产品说明书 9.3）。
    """

    def __init__(self, process_id: str, reason: str) -> None:
        self.process_id = process_id
        self.reason = reason
        super().__init__(f"流程 {process_id} 当前不可用: {reason}")
