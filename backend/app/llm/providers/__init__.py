"""供应商适配器包（T2-56）。

三类适配器按能力差异分工：`OpenAICompatProvider` 走原生结构化输出（第一层），
`PrivateModelProvider` 走提示词约束（第二层），`MockProvider` 按 schema 生成满足契约的
最小对象（供 `USE_MOCK_LLM=true` 的测试路径）。网关只依赖 `ModelProvider` 协议，因此
"换供应商"等于新增一个适配器，业务代码不动。

`app/llm/structured.py` 的三层保障在此之外：适配器只负责"把一次请求发出去、把响应
翻译回 `ModelResponse`"，重试与降级决策不在这里。
"""

from app.llm.providers.base import ModelProvider, ModelResponse, parse_structured
from app.llm.providers.mock import MockProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.providers.private_model import PrivateModelProvider

__all__ = [
    "MockProvider",
    "ModelProvider",
    "ModelResponse",
    "OpenAICompatProvider",
    "PrivateModelProvider",
    "parse_structured",
]
