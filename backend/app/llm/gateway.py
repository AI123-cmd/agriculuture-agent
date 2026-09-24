"""模型网关统一入口（T2-55 的最小实现）。

把方案 10.1 的六项职责收在一个类里：统一入口、任务路由、结构化输出、超时熔断、计量
与审计、内容审计。业务侧（智能体）只调 `chat(task_type, messages, schema)` 这一个方法。

本模块是网关的骨架：候选链遍历、熔断放行/记账、上下文裁剪、审计哈希与计量回写都已
就位。后续任务补齐的部分——T2-56 的三类真实适配器（OpenAI 兼容、私有化、Mock 已就位）、
T2-58 的按任务类型降级出口表与零模型模式、T2-59 的 `llm_call` 落库与成本估算、以及
从 `Settings` 装配 `router` 与 `providers` 的工厂函数。

网关是通用组件，它**不得导入任何业务模块**（`agent`/`gate`/`services`/`broadcast`），
这是全局约定 L3 硬规则：反向依赖会让它无法复用，也会让"网关可用性"受业务层变更牵连。
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from app.llm.circuit import CircuitBreaker
from app.llm.context import trim_messages
from app.llm.enums import TaskType
from app.llm.errors import AllProvidersFailedError, ModelUnavailableError
from app.llm.providers.base import ModelProvider, ModelResponse
from app.llm.router import ModelCandidate, ModelRouter


class CallAuditor:
    """内容审计：记录输入输出的哈希，用于事后追溯，不留原文。

    农事问诊内容含农户地块、位置、作物状况，属敏感数据。哈希让系统能回答"两次调用
    是否相同输入""某次输出与某条审计记录是否对应"，但无法从审计表还原原文。这是隐私
    保护与可追溯性的平衡点。
    """

    def __init__(self) -> None:
        self._records: list[dict[str, str]] = []

    def record_digest(self, task_type: TaskType | str, messages: list[dict]) -> str:
        """记录一次调用的输入哈希，返回 64 位十六进制的 sha256。"""
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._records.append({"task_type": str(task_type), "input_digest": digest})
        return digest

    def record_output_digest(self, digest: str, content: str | None) -> None:
        """记录输出哈希，与输入哈希配对。落库的是哈希与元数据，不是原文。"""
        out = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
        self._records.append({"input_digest": digest, "output_digest": out})

    def records(self) -> list[dict[str, str]]:
        """已记录条目（副本）。供单测断言"审计收到哈希"，也供后续任务落库。"""
        return list(self._records)


class ModelGateway:
    """模型调用统一入口。六项职责：入口、路由、结构化输出、超时熔断、计量审计、内容审计。"""

    def __init__(
        self,
        router: ModelRouter,
        providers: dict[str, ModelProvider],
        circuit: CircuitBreaker,
        meter: Any,
        auditor: CallAuditor,
    ) -> None:
        self._router = router
        self._providers = providers
        self._circuit = circuit
        self._meter = meter
        self._auditor = auditor

    async def chat(
        self,
        task_type: TaskType,
        messages: list[dict],
        schema: type[BaseModel] | None = None,
    ) -> ModelResponse:
        """所有模型调用的唯一入口。

        `schema` 非空时，`parsed` 承载结构化结果；`parsed` 为 `None` 即视为契约违约，
        由智能体基类触发重试与降级。网关不抛"解析失败"，而是把结果与错误放进响应——
        不同智能体的降级出口不同，这个决定不该由通用组件做。
        """
        decision = self._router.resolve(task_type)
        trimmed = trim_messages(messages, decision.max_history_rounds)
        digest = self._auditor.record_digest(task_type, trimmed)
        last_error: str | None = None
        for candidate in decision.chain:
            if not self._circuit.allow(candidate.provider):
                last_error = f"{candidate.provider} 熔断中"
                continue
            response = await self._invoke(candidate, trimmed, schema)
            response = response.model_copy(update={"is_fallback": candidate is not decision.chain[0]})
            if response.error is None:
                self._circuit.record_success(candidate.provider)
                self._auditor.record_output_digest(digest, response.content)
                await self._meter.record(task_type, response, candidate, is_fallback=response.is_fallback)
                return response
            last_error = response.error
            if _counts_as_failure(response.error):
                self._circuit.record_failure(candidate.provider)
        raise AllProvidersFailedError(str(task_type.value), last_error)

    async def _invoke(
        self,
        candidate: ModelCandidate,
        messages: list[dict],
        schema: type[BaseModel] | None,
    ) -> ModelResponse:
        """调用一个候选的适配器。适配器不存在视为该候选不可用，继续走链。"""
        provider = self._providers.get(candidate.provider)
        if provider is None:
            return _error_response(candidate, f"供应商未装配：{candidate.provider}")
        try:
            return await provider.chat(candidate.model, messages, schema)
        except ModelUnavailableError as exc:
            return _error_response(candidate, str(exc))


def _error_response(candidate: ModelCandidate, error: str) -> ModelResponse:
    return ModelResponse(
        content=None,
        parsed=None,
        model=candidate.model,
        provider=candidate.provider,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        is_fallback=False,
        error=error,
    )


def _counts_as_failure(error: str | None) -> bool:
    """该错误是否计入熔断。

    超时、限流、5xx 计入：它们是供应商可用性问题。400/401 是配置问题，重试无意义，
    计入熔断会让一个其实可用的供应商被误熔断；解析失败是输出质量或 Schema 问题，
    同样不计入（技术方案 10.2 的错误分类表）。
    """
    if not error:
        return False
    if error == "parse_error":
        return False
    if error.startswith("http_4") and not error.startswith("http_429"):
        return False
    return True


__all__ = ["CallAuditor", "ModelGateway"]
