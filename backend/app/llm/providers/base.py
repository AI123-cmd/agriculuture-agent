"""供应商适配接口与统一响应模型（T2-56）。

这是网关与实际模型服务之间的唯一接口面：换供应商等于新增一个适配器，而不是改业务
代码。本模块只定义协议与数据形态，不含任何 HTTP 细节——具体适配器在
`app/llm/providers/` 下，按能力差异（是否支持原生结构化输出）分别实现。

`ModelResponse.error` 非空即表示这次调用失败，但**网关不抛"解析失败"**：解析结果与
错误都放进响应里，由调用方（智能体基类）决定降级出口——不同智能体的降级语义不同，
网关这个通用组件不该替它们做决定。
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict


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


__all__ = ["ModelProvider", "ModelResponse"]
