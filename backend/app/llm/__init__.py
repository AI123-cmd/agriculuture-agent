"""模型网关包（T2-55）。

对外只暴露三类东西：入口 `ModelGateway`、任务类型（`TaskType` 的镜像定义，见
`app/llm/enums.py` 的说明）、以及错误类型。业务侧不直接接触适配器与路由器。

本包受全局约定 L3 约束：不得导入 `app.agent`、`app.gate`、`app.services`。
"""

from app.llm.circuit import CircuitBreaker, CircuitState
from app.llm.context import trim_messages
from app.llm.enums import TaskType
from app.llm.errors import (
    AllProvidersFailedError,
    ModelError,
    ModelUnavailableError,
    SchemaUnsupportedError,
    TransientModelError,
)
from app.llm.gateway import CallAuditor, ModelGateway
from app.llm.metering import NullTokenMeter, TokenMeter
from app.llm.providers.base import ModelProvider, ModelResponse
from app.llm.router import ModelCandidate, ModelRouter, RoutingDecision
from app.llm.tiers import DEFAULT_TIER_MAP, ModelTier, TierSpec

__all__ = [
    "DEFAULT_TIER_MAP",
    "AllProvidersFailedError",
    "CallAuditor",
    "CircuitBreaker",
    "CircuitState",
    "ModelCandidate",
    "ModelError",
    "ModelGateway",
    "ModelProvider",
    "ModelResponse",
    "ModelRouter",
    "ModelTier",
    "ModelUnavailableError",
    "NullTokenMeter",
    "RoutingDecision",
    "SchemaUnsupportedError",
    "TaskType",
    "TierSpec",
    "TokenMeter",
    "TransientModelError",
    "trim_messages",
]
