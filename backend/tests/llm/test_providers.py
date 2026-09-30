"""T2-56：供应商适配器与结构化输出的三层保障。

用例分五段：

1. **请求构造**——OpenAI 兼容适配器在 `response_schema` 非空时注入原生结构化输出参数
   （`response_format=json_schema`）；私有化适配器改为把 Schema 说明追加进 messages，
   请求体里刻意不出现 `response_format`（对不支持它的端点发送会被判 400）。
2. **响应解析与错误分类**——按 OpenAI 响应取 `choices[0].message.content`，按私有化响应
   兼容 `answer`；超时、429、401 分别映射到 `timeout` / `http_429` / `http_401`。
3. **Mock 适配器**——对任意 schema 生成满足契约的对象，不发网络请求。
4. **三层保障**——层标记 `native` / `repair` / `degrade`；只重试一次；修复提示携带首次
   失败文本；**绝不正则硬抽**——非 JSON 的"共 2 条建议，间隔 7 天"解析为 `None` 而不是
   抽出了那个数字。
5. **与熔断的衔接**——解析失败不计入熔断，供应商不会被误熔断；`USE_MOCK_LLM=true` 不构造
   任何真实适配器。

HTTP 层用 `respx` 拦截，断言实际发出的请求体；三层保障用脚本化替身，因为它是编排逻辑，
与"走 HTTP 还是走 mock provider"无关。
"""

import json
import time
from typing import Literal

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.llm.circuit import CircuitBreaker, CircuitState
from app.llm.errors import AllProvidersFailedError
from app.llm.factory import build_gateway
from app.llm.gateway import CallAuditor, ModelGateway
from app.llm.metering import NullTokenMeter
from app.llm.providers.base import ModelResponse, elapsed_ms, parse_structured
from app.llm.providers.mock import MockProvider, _default_for_schema
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.providers.private_model import PrivateModelProvider, _schema_instruction
from app.llm.router import ModelCandidate, RoutingDecision
from app.llm.structured import LAYER_DEGRADE, LAYER_NATIVE, LAYER_REPAIR, StructuredOutputGuard
from app.llm.tiers import ModelTier
from app.llm.enums import TaskType


class _Sample(BaseModel):
    """最小契约：必填的字符串与整数。"""

    name: str
    count: int


class _Inner(BaseModel):
    kind: str
    value: int


class _Complex(BaseModel):
    """含枚举、数组、可空的契约，用来验证 Mock 的通用填充。"""

    mode: Literal["a", "b"]
    items: list[_Inner] = Field(default_factory=list)
    note: str | None = None


class _Dosage(BaseModel):
    """处方类契约：数值字段一旦被编造就会误导用药。"""

    dosage_max: int
    safety_interval_days: int


_OPENAI_BASE = "https://llm.example.com/v1"
_PRIVATE_BASE = "https://private.example.com/v1"
_MESSAGES = [{"role": "user", "content": "叶子枯了"}]


def _openai() -> OpenAICompatProvider:
    return OpenAICompatProvider(_OPENAI_BASE, "sk-test")


def _private(api_key: str = "") -> PrivateModelProvider:
    return PrivateModelProvider(_PRIVATE_BASE, api_key)


def _openai_body(content: str, *, model: str = "vendor-model") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _sent_json(respx_mock: respx.Router, index: int = 0) -> dict:
    return json.loads(respx_mock.calls[index].request.content)


# --------------------------------------------------- 一、请求构造


async def test_openai_compat_injects_native_response_format(respx_mock: respx.Router) -> None:
    """`response_schema` 非空时请求体含 `response_format`，类型为 json_schema 且 strict。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_openai_body('{"name":"n","count":3}'))
    )

    response = await _openai().chat("m", _MESSAGES, _Sample)

    payload = _sent_json(respx_mock)
    assert "response_format" in payload
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["name"] == "_Sample"
    assert response.parsed == _Sample(name="n", count=3)


async def test_openai_compat_without_schema_omits_response_format(respx_mock: respx.Router) -> None:
    """`response_schema` 为空时不注入 `response_format`，只返回文本。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_openai_body("纯文本回复"))
    )

    response = await _openai().chat("m", _MESSAGES)

    assert "response_format" not in _sent_json(respx_mock)
    assert response.parsed is None
    assert response.content == "纯文本回复"


async def test_private_model_appends_schema_instruction_without_response_format(
    respx_mock: respx.Router,
) -> None:
    """私有化适配器把 Schema 说明追加进 messages，请求体不含 `response_format`。"""
    respx_mock.post(f"{_PRIVATE_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={"answer": '{"name":"n","count":1}'})
    )

    await _private().chat("m", _MESSAGES, _Sample)

    payload = _sent_json(respx_mock)
    assert "response_format" not in payload
    assert payload["temperature"] == 0.0
    last = payload["messages"][-1]
    assert last["role"] == "system"
    assert "JSON Schema" in last["content"]
    assert "name" in last["content"]


async def test_openai_compat_temperature_is_zero(respx_mock: respx.Router) -> None:
    """结构化输出场景固定 `temperature=0.0`，降低格式随机性。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_openai_body('{"name":"n","count":1}'))
    )

    await _openai().chat("m", _MESSAGES, _Sample)

    assert _sent_json(respx_mock)["temperature"] == 0.0


def test_private_schema_instruction_mentions_schema() -> None:
    """私有化的提示词约束原文包含目标 Schema，让模型知道要输出什么。"""
    instruction = _schema_instruction(_Sample)

    assert "JSON Schema" in instruction
    assert "name" in instruction and "count" in instruction


# --------------------------------------------------- 二、响应解析与错误分类


async def test_timeout_maps_to_error_timeout(respx_mock: respx.Router) -> None:
    """超时返回 `error="timeout"`，不抛异常。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(side_effect=httpx.TimeoutException("慢"))

    response = await _openai().chat("m", _MESSAGES, _Sample)

    assert response.error == "timeout"
    assert response.parsed is None
    assert response.content is None


async def test_http_429_maps_to_error(respx_mock: respx.Router) -> None:
    """429 限流返回 `error="http_429"`。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(429, json={}))

    response = await _openai().chat("m", _MESSAGES, _Sample)

    assert response.error == "http_429"


async def test_http_401_maps_to_error(respx_mock: respx.Router) -> None:
    """401 鉴权失败返回 `error="http_401"`（配置问题，重试无意义）。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(401, json={}))

    response = await _openai().chat("m", _MESSAGES, _Sample)

    assert response.error == "http_401"


async def test_reads_usage_and_latency(respx_mock: respx.Router) -> None:
    """从响应取 token 用量，`latency_ms` 为非负整数。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_openai_body('{"name":"n","count":1}'))
    )

    response = await _openai().chat("m", _MESSAGES, _Sample)

    assert response.prompt_tokens == 11
    assert response.completion_tokens == 7
    assert isinstance(response.latency_ms, int) and response.latency_ms >= 0
    assert response.provider == "openai_compat"


# --------------------------------------------------- 三、Mock 适配器


async def test_mock_provider_satisfies_arbitrary_schema() -> None:
    """Mock 对任意 schema 返回满足契约的 `parsed`，且解析出的对象可用。"""
    provider = MockProvider()

    simple = await provider.chat("m", _MESSAGES, _Sample)
    complex_ = await provider.chat("m", _MESSAGES, _Complex)

    assert isinstance(simple.parsed, _Sample)
    assert isinstance(complex_.parsed, _Complex)
    assert complex_.parsed.mode == "a"
    assert complex_.parsed.items == []
    assert simple.error is None


async def test_mock_provider_without_schema_returns_text() -> None:
    """无 schema 时返回一段文本，`parsed` 为 None。"""
    response = await MockProvider().chat("m", _MESSAGES)

    assert response.parsed is None
    assert response.content


def test_default_for_schema_none_returns_none() -> None:
    """schema 为空时生成器返回 None，不做无谓推断。"""
    assert _default_for_schema(None) is None


# --------------------------------------------------- 四、三层保障


class _ScriptedProvider:
    """按脚本回放 `ModelResponse` 的适配器替身。"""

    name = "scripted"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls: list[list[dict]] = []

    async def chat(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
        timeout: float = 30.0,
    ) -> ModelResponse:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


def _parsed_response() -> ModelResponse:
    return ModelResponse(
        content='{"name":"n","count":1}',
        parsed=_Sample(name="n", count=1),
        model="m",
        provider="scripted",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
    )


def _parse_failure_response(detail: str = "parse_error") -> ModelResponse:
    return ModelResponse(
        content='{"name":"n"}',
        parsed=None,
        model="m",
        provider="scripted",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
        error=detail,
    )


async def test_layer_native_when_first_parses() -> None:
    """首次即解析成功时层标记为 `native`，只调用一次。"""
    provider = _ScriptedProvider([_parsed_response()])

    response, layer = await StructuredOutputGuard().chat_with_structured(provider, "m", _MESSAGES, _Sample, 30.0)

    assert layer == LAYER_NATIVE
    assert response.parsed == _Sample(name="n", count=1)
    assert len(provider.calls) == 1


async def test_layer_repair_when_second_succeeds() -> None:
    """首次解析失败、第二次成功时层标记为 `repair`。"""
    provider = _ScriptedProvider([_parse_failure_response(), _parsed_response()])

    response, layer = await StructuredOutputGuard().chat_with_structured(provider, "m", _MESSAGES, _Sample, 30.0)

    assert layer == LAYER_REPAIR
    assert response.parsed == _Sample(name="n", count=1)
    assert len(provider.calls) == 2


async def test_layer_degrade_when_both_fail() -> None:
    """两次都解析失败时层标记为 `degrade`，把降级语义交给智能体基类。"""
    provider = _ScriptedProvider([_parse_failure_response(), _parse_failure_response()])

    response, layer = await StructuredOutputGuard().chat_with_structured(provider, "m", _MESSAGES, _Sample, 30.0)

    assert layer == LAYER_DEGRADE
    assert response.parsed is None


async def test_repair_prompt_carries_first_error_text() -> None:
    """第二次调用携带修复提示，且提示中含首次的校验错误文本。"""
    detail = "1 validation error for _Dosage\n dosage_max\n  Field required"
    provider = _ScriptedProvider([_parse_failure_response(detail), _parsed_response()])

    await StructuredOutputGuard().chat_with_structured(provider, "m", _MESSAGES, _Sample, 30.0)

    repair_messages = provider.calls[1]
    assert len(repair_messages) > len(_MESSAGES)
    assert any(detail in str(m.get("content", "")) for m in repair_messages)
    assert repair_messages[-1]["role"] == "user"


async def test_retries_at_most_once() -> None:
    """永远返回格式错误的供应商总调用次数为 2（只重试一次）。"""
    provider = _ScriptedProvider([_parse_failure_response(), _parse_failure_response()])

    await StructuredOutputGuard().chat_with_structured(provider, "m", _MESSAGES, _Sample, 30.0)

    assert len(provider.calls) == 2


async def test_no_regex_extraction_on_parse_failure(respx_mock: respx.Router) -> None:
    """非 JSON 但含数字的输出解析为 None，绝不把 2 或 7 硬抽进契约字段。"""
    respx_mock.post(f"{_OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_openai_body("共 2 条建议，间隔 7 天"))
    )

    response = await _openai().chat("m", _MESSAGES, _Dosage)

    assert response.parsed is None
    assert response.error == "parse_error"
    assert response.content == "共 2 条建议，间隔 7 天"


def test_parse_structured_never_guesses() -> None:
    """`parse_structured` 对非法 JSON 与不合契约的 JSON 都返回 `parse_error`。"""
    assert parse_structured("共 2 条建议，间隔 7 天", _Dosage) == (None, "parse_error")
    assert parse_structured('{"dosage_max": 2}', _Dosage) == (None, "parse_error")
    parsed, error = parse_structured('{"dosage_max": 2, "safety_interval_days": 7}', _Dosage)
    assert error is None
    assert parsed == _Dosage(dosage_max=2, safety_interval_days=7)


# --------------------------------------------------- 五、与熔断的衔接与边界


class _StaticRouter:
    """固定返回一个单跳候选链的路由替身。"""

    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision

    def resolve(self, task_type: object, *, budget_remaining: int | None = None) -> RoutingDecision:
        return self._decision


def _single_candidate_gateway(provider_name: str, provider: object, threshold: int) -> tuple[ModelGateway, CircuitBreaker]:
    decision = RoutingDecision(
        task_type=TaskType.TRIAGE,
        tier=ModelTier.SMALL,
        chain=(ModelCandidate(provider=provider_name, model="m", tier=ModelTier.SMALL),),
        max_tokens=2048,
        max_history_rounds=6,
    )
    circuit = CircuitBreaker(failure_threshold=threshold, cooldown_seconds=30, half_open_probes=2, clock=lambda: 0.0)
    gateway = ModelGateway(
        router=_StaticRouter(decision),
        providers={provider_name: provider},
        circuit=circuit,
        meter=NullTokenMeter(),
        auditor=CallAuditor(),
    )
    return gateway, circuit


async def test_parse_failure_does_not_trip_circuit() -> None:
    """连续三次解析失败后供应商仍放行：解析失败是输出质量问题，不是可用性问题。"""
    provider = _ScriptedProvider([_parse_failure_response()])
    gateway, circuit = _single_candidate_gateway("p1", provider, threshold=1)

    for _ in range(3):
        with pytest.raises(AllProvidersFailedError):
            await gateway.chat(TaskType.TRIAGE, _MESSAGES, _Sample)

    assert circuit.state_of("p1") is CircuitState.CLOSED
    assert circuit.allow("p1") is True


def test_elapsed_ms_is_non_negative() -> None:
    """`elapsed_ms` 对负的时间差返回 0，保证 `latency_ms` 非负。"""
    assert elapsed_ms(time.perf_counter() + 100.0) == 0


def test_model_response_nine_fields_and_defaults() -> None:
    """九个字段齐全，`is_fallback` 默认 False，`content` 与 `parsed` 可分别为 None。"""
    response = ModelResponse(
        content=None,
        parsed=None,
        model="m",
        provider="p",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
    )

    assert set(response.model_dump().keys()) == {
        "content",
        "parsed",
        "model",
        "provider",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "is_fallback",
        "error",
    }
    assert response.is_fallback is False
    assert response.error is None
    assert response.content is None and response.parsed is None


def test_mock_mode_does_not_build_real_adapters() -> None:
    """`USE_MOCK_LLM=true` 下装配出的供应商全部是 `MockProvider`，不含真实适配器。"""
    gateway = build_gateway()

    providers = gateway._providers  # noqa: SLF001 - 断言装配结果必须读内部字典
    assert providers
    assert all(isinstance(p, MockProvider) for p in providers.values())
    assert not any(isinstance(p, (OpenAICompatProvider, PrivateModelProvider)) for p in providers.values())


def test_non_mock_mode_does_not_silently_fall_back(monkeypatch) -> None:
    """`USE_MOCK_LLM=false` 时显式报错，不静默回落到 Mock。"""
    monkeypatch.setenv("USE_MOCK_LLM", "false")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            build_gateway()
    finally:
        get_settings.cache_clear()
