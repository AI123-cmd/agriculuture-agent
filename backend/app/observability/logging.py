"""结构化日志：处理器链、trace 上下文注入与统一脱敏（T6-08，技术方案 14.4）。

业务代码只从 `app.observability` 取 `log`，不直接用标准库 `logging` 与 `print`——统一的
入口是"日志与 trace 只经一个序列化器落盘"这条工程约束能够成立的前提，脱敏正挂在这条链上。

### 处理器顺序是硬约束

脱敏处理器（`_mask_event`）**必须排在渲染器之前**。顺序反了，JSON 体已经成型，脱敏只能
对整个字符串做正则替换，嵌套结构处理不了（`prescription.facts_provenance.phone` 会漏）。
`test_logging.py` 用嵌套用例把这条顺序钉死。

### 与标准库 logging 的关系

处理器链只作用于 structlog。但项目里仍有第三方库（uvicorn、SQLAlchemy）走标准库 logging，
其输出也需要脱敏。因此 `configure_logging` 额外给根 logger 挂一个
`_MaskingFormatter`：标准库侧的日志经它格式化，同样过一遍脱敏。两条通路共用同一个 masker
实例，规则不会分叉。

### 生产按 `ENV` 分档

生产用 JSON（便于审计检索），非生产用 console（便于本地阅读）。`ensure_ascii=False` 是必需
的，否则中文全部转义成 `\uXXXX`，日志不可读。级别由 `LOG_LEVEL` 控制。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from app.observability.masker import ObservabilityMasker

# 懒加载代理：首次调用时才按当时的 structlog 配置构造具体 logger，因此本模块可以在
# `configure_logging` 之前被导入（测试与被导入的节点都会在导入期取到它）。
log: Any = structlog.get_logger("app")

# 当前生效的脱敏器。`configure_logging` 装配后由标准库侧的 formatter 复用，避免两处规则分叉。
_masker = ObservabilityMasker()

# 标准库侧的脱敏处理器。之所以**追加**而不是替换根 logger 的处理器列表：pytest 的
# `caplog` 也挂在根 logger 上，替换会把它清掉，使既有用例的日志断言失效。
_stdlib_handler: logging.Handler | None = None


def add_trace_context(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """把当前 trace 的 `trace_id` / `span_id` 注入事件字典（T6-06 提供取值）。

    取值函数在调用时才导入：`app.observability.tracing` 与本模块同包，导入期互相引用会形成
    环；延迟到运行期导入既避开环，也保证拿到的是当前（可能已被测试重置的）provider。
    """
    from app.observability.tracing import current_trace_context

    ctx = current_trace_context()
    if ctx is not None:
        event_dict.setdefault("trace_id", ctx["trace_id"])
        event_dict.setdefault("span_id", ctx["span_id"])
    return event_dict


def _mask_event(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """脱敏处理器：在渲染之前对整个事件字典递归脱敏。"""
    return _masker.mask(event_dict)


class _MaskingFormatter(logging.Formatter):
    """标准库 logging 侧的脱敏：先按常规格式化，再对结果字符串做手机号/设备号清洗。

    标准库记录已被格式化成字符串，无法再做结构级脱敏，因此这里只做字符串级清洗。手机号
    模式足够具体，全局替换安全；设备号模式较宽，为免误伤摘要类长串，此处只清手机号。
    """

    def format(self, record: logging.LogRecord) -> str:
        return _masker.mask_phone(super().format(record))


def configure_logging(settings: Any) -> None:
    """装配 structlog 处理器链与标准库 logging。幂等：重复调用只覆盖配置。

    `mask_geo` 取 `ENV != "production"`：本地调试需要真实坐标，生产启动检查应拒绝
    `mask_geo=False`（见 `startup_check`）。这样"开发方便"与"生产从严"各得其所。
    """
    global _masker
    _masker = ObservabilityMasker(
        extra_fields=tuple(getattr(settings, "MASK_EXTRA_FIELDS", ()) or ()),
        mask_geo=_resolve_mask_geo(settings),
    )

    level = _resolve_level(settings)
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if getattr(settings, "LOG_FORMAT", "json") == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # 绑定 consultation_id / user_id 等上下文
            add_trace_context,  # trace_id / span_id
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # 异常堆栈必须在脱敏之前渲染成文本：`logger.exception` 记录的是 `exc_info`，
            # 只有渲染成字符串后，堆栈里可能夹带的手机号才会被同一个脱敏处理器清洗。
            structlog.processors.format_exc_info,
            _mask_event,  # 脱敏必须早于渲染器
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    _configure_stdlib(level)


def _resolve_mask_geo(settings: Any) -> bool:
    """坐标是否脱敏：显式配置优先，未配置时按 `ENV` 派生（生产脱敏）。"""
    explicit = getattr(settings, "MASK_GEO", None)
    if explicit is not None:
        return bool(explicit)
    return getattr(settings, "ENV", "development") == "production"


def _resolve_level(settings: Any) -> int:
    """把 `LOG_LEVEL`（字符串或整数）解析成 logging 级别；非法值回退 INFO。"""
    raw = getattr(settings, "LOG_LEVEL", "INFO")
    if isinstance(raw, int):
        return raw
    level = logging.getLevelNamesMapping().get(str(raw).upper())
    return level if level is not None else logging.INFO


def _configure_stdlib(level: int) -> None:
    """给根 logger 追加一个带脱敏的流处理器，使第三方库的标准库日志同样被清洗。

    追加而非替换：替换会清掉 pytest `caplog` 等外部挂在根 logger 上的处理器。用模块级
    引用去重，使重复调用 `configure_logging`（如多次进 lifespan）不会叠加处理器。
    """
    global _stdlib_handler
    root = logging.getLogger()
    if _stdlib_handler is None:
        _stdlib_handler = logging.StreamHandler()
        _stdlib_handler.setFormatter(_MaskingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    if _stdlib_handler not in root.handlers:
        root.addHandler(_stdlib_handler)
    root.setLevel(level)


def bind_context(**fields: Any) -> None:
    """在问诊入口绑定上下文（`consultation_id` / `user_id` / `process_id`）。

    用 `contextvars` 承载：一次问诊的日志分散在多个节点与异步任务里，逐个传 logger 不现实，
    而 asyncio 任务继承 contextvars，绑定一次即全链路可见。
    """
    structlog.contextvars.bind_contextvars(**fields)


@contextmanager
def bound_context(**fields: Any) -> Iterator[None]:
    """绑定上下文的上下文管理器：退出时只还原本次绑定，不影响更外层（如 `request_id`）。

    比 `bind_context` + `clear_context` 更适合"一段逻辑内有效"的场景：后者会清掉全部上下文，
    把请求级绑定的字段一并抹去。`contextvars` 的令牌回滚使嵌套绑定互不干扰。
    """
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def clear_context() -> None:
    """清理全部上下文。请求结束必须调用，否则跨请求串号（日志带上错误的 `consultation_id`）。"""
    structlog.contextvars.clear_contextvars()


__all__ = [
    "add_trace_context",
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "log",
]
