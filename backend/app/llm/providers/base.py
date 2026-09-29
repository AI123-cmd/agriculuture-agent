"""供应商适配接口与统一响应模型（T2-56）。

这是网关与实际模型服务之间的唯一接口面：换供应商等于新增一个适配器，而不是改业务
代码。本模块只定义协议与数据形态，不含任何 HTTP 细节——具体适配器在
`app/llm/providers/` 下，按能力差异（是否支持原生结构化输出）分别实现。

`ModelResponse.error` 非空即表示这次调用失败，但**网关不抛"解析失败"**：解析结果与
错误都放进响应里，由调用方（智能体基类）决定降级出口——不同智能体的降级语义不同，
网关这个通用组件不该替它们做决定。
"""

import json
import time
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError


class ModelResponse(BaseModel):
    """一次模型调用的统一响应。九个字段各有用途，缺一不可。

    - `content`：原始文本，无结构化需求时使用，也是审计哈希的输入；
    - `parsed`：解析后的契约对象，智能体直接消费；为 `None` 即视为契约违约；
    - `model` / `provider`：进计量表用于成本归属；
    - `prompt_tokens` / `completion_tokens`：成本与预算的输入；
    - `latency_ms`：进链路指标；
    - `is_fallback`：是否为降级链上的备选，由**网关**在调用后设置（适配器不知道
      自己在链上的位置）；
    - `error`：失败原因，`parsed` 与 `content` 均为空时非空。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None
    parsed: BaseModel | None
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    is_fallback: bool = False
    error: str | None = None


class ModelProvider(Protocol):
    """模型供应商适配接口。

    适配器负责两件事：把统一入参翻译成该供应商的请求体，把响应翻译回
    `ModelResponse`。错误分类（超时/限流/5xx 可重试，400/401 不可重试）也在适配器内
    完成，网关与熔断器只读 `error` 字段的取值。
    """

    name: str

    async def chat(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
        timeout: float = 30.0,
    ) -> ModelResponse: ...


def parse_structured(content: str | None, schema: type[BaseModel] | None) -> tuple[BaseModel | None, str | None]:
    """把模型返回的原始文本解析成契约对象。返回 `(parsed, error)`。

    三层保障里，这里承担"解析"这一步，本身不做任何补救：解析失败一律返回
    `error="parse_error"`，由 `app/llm/structured.py` 决定是否构造修复提示重试。
    **绝不回退到正则硬抽**——农业场景下错误的字段值比没有字段值更危险，从文本里
    猜出来的剂量数字会被农户照着用（技术方案 10.2）。
    """
    if schema is None:
        return None, None
    if not content:
        return None, "parse_error"
    try:
        payload: Any = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, "parse_error"
    try:
        return schema.model_validate(payload), None
    except ValidationError:
        return None, "parse_error"


def build_error_response(provider: str, model: str, error: str, latency_ms: int) -> ModelResponse:
    """构造一条失败响应。token 记 0：失败响应没有可信的 token 数。"""
    return ModelResponse(
        content=None,
        parsed=None,
        model=model,
        provider=provider,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=latency_ms,
        is_fallback=False,
        error=error,
    )


def elapsed_ms(started: float) -> int:
    """从 `time.perf_counter()` 起点算到现在的毫秒数（非负整数）。"""
    return max(0, int((time.perf_counter() - started) * 1000))


__all__ = [
    "ModelProvider",
    "ModelResponse",
    "build_error_response",
    "elapsed_ms",
    "parse_structured",
]
