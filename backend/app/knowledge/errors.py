"""知识域错误类型。

本模块分两组：

- **检索可用性**（T2-49）：`KnowledgeUnavailableError` —— 检索真不可用（数据库连接失败、
  向量化服务超时）时抛本异常，由调用方（证据核查智能体 T2-18）捕获并返回空集。检索本身
  **不做重试**：向量化服务若超时，立即重试大概率仍超时，反而把问诊延迟拉长十几秒；需要
  重试的语义由上层统一处理，不在检索实现里各写一份。

- **核准状态机**（T2-54）：`KnowledgeNotFound` / `IllegalTransition` / `ReasonRequired` ——
  由 `KnowledgeReviewService` 抛出。三者都继承 `AppException`，使 `main.py` 的全局 handler
  直接用 `exc.code` 得到对应 HTTP 状态（404 / 409 / 400），无需在路由层再翻译一遍。
"""

from app.core.exceptions import AppException


class KnowledgeUnavailableError(Exception):
    """知识检索不可用。与"查询合法但未命中"区分：后者返回空列表。"""


class KnowledgeNotFound(AppException):
    """待变更的知识条目不存在。

    取 404：条目 id 由后台运营人工给出，写错 id 是调用方可修正的请求错误，不应是 500。
    """

    def __init__(self, knowledge_id: str) -> None:
        super().__init__(404, f"知识条目不存在：{knowledge_id}")


class IllegalTransition(AppException):
    """非法的状态迁移（如 `draft` 直接 `suspend`、对已 `approved` 条目再次 `approve`）。

    取 409 而非 400：目标状态与当前状态冲突，是"状态前置条件不满足"的并发语义，
    与 `ConflictError` 同类——重试前需先读回最新状态，而不是改请求体即可通过。
    """

    def __init__(self, message: str) -> None:
        super().__init__(409, message)


class ReasonRequired(AppException):
    """`suspend` / `reject` / `deprecate` 未给出理由。

    取 400：理由由调用方补上即可，属请求参数缺失。
    """

    def __init__(self, message: str) -> None:
        super().__init__(400, message)


class OperatorRequired(AppException):
    """核准操作未给出 `operator_id`。

    取 400：没有操作人的变更无法追溯，接口在进入状态机前就拒绝。它是请求参数缺失，
    与"理由缺失"分开成类，使"忘了传操作人"与"忘了写理由"在日志里可区分。
    """

    def __init__(self, message: str = "核准操作必须给出 operator_id") -> None:
        super().__init__(400, message)


__all__ = [
    "IllegalTransition",
    "KnowledgeNotFound",
    "KnowledgeUnavailableError",
    "OperatorRequired",
    "ReasonRequired",
]
