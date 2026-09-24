"""模型层异常（T2-55 / T2-56）。

异常分两类，分界线是"再试一次是否有意义"：

- `TransientModelError` 及其子类：超时、限流、服务端 5xx、候选链全部失败。这类失败
  换一次调用可能成功，智能体基类（T2-13）的 `retry_if_exception_type` 会重试它。
- 其余（`SchemaUnsupportedError`、`ModelUnavailableError` 中的配置类原因）：重试不会
  改变结果，只会多花 token 与延迟，因此直接走降级。

`AllProvidersFailedError` 继承 `TransientModelError` 而不是与之并列：候选链全失败是
"当前这一跳没成功"，换一轮调用仍可能成功（供应商可能已恢复），因此它必须落进可重试
的那一类。
"""


class ModelError(Exception):
    """模型层异常基类。"""


class TransientModelError(ModelError):
    """瞬时故障：重试可能成功。智能体基类据此决定是否重试。"""


class ModelUnavailableError(ModelError):
    """供应商不可用：未配置、已熔断或被显式禁用。重试无意义。"""


class AllProvidersFailedError(TransientModelError):
    """候选链上的供应商全部失败（或全部熔断中）。

    携带最后一次的失败原因，便于日志定位；智能体基类捕获它后按任务类型的降级出口
    处理（T2-58 的 `TASK_DEGRADE_EXIT`）。
    """

    def __init__(self, task_type: str, last_error: str | None = None) -> None:
        detail = last_error or "无更多候选供应商"
        super().__init__(f"任务 {task_type} 的候选供应商全部失败：{detail}")
        self.task_type = task_type
        self.last_error = last_error


class SchemaUnsupportedError(ModelError):
    """供应商不支持该 schema 的结构化输出。

    出现它说明档位配置与供应商能力不匹配，属配置问题；重试无意义，应直接降级并告警。
    """


__all__ = [
    "AllProvidersFailedError",
    "ModelError",
    "ModelUnavailableError",
    "SchemaUnsupportedError",
    "TransientModelError",
]
