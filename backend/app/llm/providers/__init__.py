"""供应商适配器包（T2-56）。

目前已就位的是 `MockProvider`。真实适配器（`OpenAICompatProvider`、
`PrivateModelProvider`）与结构化输出的三层保障在 T2-56 的任务范围内补齐；本阶段只
需要"能在 `USE_MOCK_LLM=true` 下跑通全部契约校验与降级路径"这一最小可用面。
"""

from app.llm.providers.base import ModelProvider, ModelResponse
from app.llm.providers.mock import MockProvider

__all__ = ["MockProvider", "ModelProvider", "ModelResponse"]
