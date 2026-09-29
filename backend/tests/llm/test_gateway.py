"""T2-55：模型网关统一入口的六项职责。

用例分四段：

1. **入口与路由**——签名与方案 10.1 一致、按 `task_type` 走 `router.resolve`、mock
   模式下 `parsed` 满足传入的 `schema`。
2. **候选链与熔断**——链上首个失败自动尝试下一个、熔断中的供应商被跳过且不发调用、
   全部失败抛 `AllProvidersFailedError`；失败尝试计入熔断器，但计量只写成功与最终
   失败各一次。
3. **上下文裁剪与审计**——超轮次裁剪保留 `system`，`trim_messages` 是纯函数；
   审计只收哈希、不留原文。
4. **边界**——`schema` 可省略、网关不导入任何业务模块、mock 模式零网络请求、
   非 mock 且未装配适配器时显式报错（不静默回落）。

替身（`_RecordingRouter` / `_ScriptedProvider` / `_RecordingMeter`）而不是 `respx`
注册响应：网关的编排逻辑与"它内部走 HTTP 还是走 mock provider"无关，替身能把候选链
逐跳的行为钉死；再用一条 `respx` 零请求断言证明它不会绕过适配器自己发包。
"""

import ast
import hashlib
import inspect
import json
import pathlib

import pytest
import respx
from pydantic import BaseModel

from app.llm import ModelGateway, TaskType, build_gateway
from app.llm.circuit import CircuitBreaker, CircuitState
from app.llm.context import trim_messages
from app.llm.errors import AllProvidersFailedError, ModelUnavailableError
from app.llm.gateway import CallAuditor
from app.llm.providers.base import ModelResponse
from app.llm.router import ModelCandidate, RoutingDecision
from app.llm.tiers import ModelTier
from app.core.config import get_settings


class _Sample(BaseModel):
    """用于验证结构化输出的最小契约：必填的字符串与整数。"""

    name: str
    count: int


def _ok(*, content: str = "ok", provider: str = "p1", model: str = "m") -> ModelResponse:
    return ModelResponse(
        content=content,
        parsed=None,
        model=model,
        provider=provider,
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=5,
    )


def _fail(*, error: str = "http_500", provider: str = "p1", model: str = "m") -> ModelResponse:
    return ModelResponse(
        content=None,
        parsed=None,
        model=model,
        provider=provider,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        error=error,
    )


class _RecordingRouter:
    """记录 `resolve` 的入参，返回固定决策。"""

    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision
        self.calls: list[object] = []

    def resolve(self, task_type: object, *, budget_remaining: int | None = None) -> RoutingDecision:
        self.calls.append(task_type)
        return self._decision


class _ScriptedProvider:
    """按脚本回放响应或异常的适配器替身。"""

    def __init__(
        self,
        name: str,
        *,
        responses: list[ModelResponse | Exception] | None = None,
    ) -> None:
        self.name = name
        self._responses = responses or [_ok(provider=name)]
        self.calls: list[dict] = []

    async def chat(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
        timeout: float = 30.0,
    ) -> ModelResponse:
        self.calls.append({"model": model, "messages": messages, "schema": response_schema})
        result = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result.model_copy()


class _RecordingMeter:
    """记录每次计量的入参。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ModelResponse, ModelCandidate, bool]] = []

    async def record(
        self,
        task_type: object,
        response: ModelResponse,
        candidate: ModelCandidate,
        *,
        is_fallback: bool = False,
    ) -> None:
        self.calls.append((task_type, response, candidate, is_fallback))


def _decision(*providers: str, max_rounds: int = 6) -> RoutingDecision:
    chain = tuple(ModelCandidate(provider=p, model=f"model-{p}", tier=ModelTier.SMALL) for p in providers)
    return RoutingDecision(
        task_type=TaskType.TRIAGE,
        tier=ModelTier.SMALL,
        chain=chain,
        max_tokens=2048,
        max_history_rounds=max_rounds,
    )


def _build(
    *providers: str,
    responses: dict[str, list[ModelResponse | Exception]] | None = None,
    max_rounds: int = 6,
    threshold: int = 5,
):
    """按候选链装配网关与替身，返回网关及所有可供断言的部件。"""
    router = _RecordingRouter(_decision(*providers, max_rounds=max_rounds))
    instances = {p: _ScriptedProvider(p, responses=(responses or {}).get(p)) for p in providers}
    # 常量时钟：`opened_at` 恒为 0、冷却期未过，熔断态不会自动转半开，断言稳定。
    circuit = CircuitBreaker(failure_threshold=threshold, cooldown_seconds=30, half_open_probes=2, clock=lambda: 0.0)
    meter = _RecordingMeter()
    auditor = CallAuditor()
    gateway = ModelGateway(router=router, providers=instances, circuit=circuit, meter=meter, auditor=auditor)
    return gateway, router, instances, circuit, meter, auditor


_MESSAGES = [{"role": "user", "content": "叶子枯了"}]


# --------------------------------------------------- 一、入口与路由


def test_chat_signature_matches_spec() -> None:
    """方案 10.1 的入口签名：`chat(task_type, messages, schema)`，`schema` 可省略。"""
    params = list(inspect.signature(ModelGateway.chat).parameters)

    assert params == ["self", "task_type", "messages", "schema"]
    assert inspect.signature(ModelGateway.chat).parameters["schema"].default is None


async def test_router_resolve_called_with_task_type() -> None:
    """按 `task_type` 走路由：网关不得自己硬编码档位。"""
    gateway, router, _, _, _, _ = _build("p1")

    await gateway.chat(TaskType.DIAGNOSE, _MESSAGES)

    assert router.calls == [TaskType.DIAGNOSE]


async def test_mock_mode_parsed_satisfies_schema() -> None:
    """`USE_MOCK_LLM=true` 时返回满足 `schema` 的结构化对象，不发真实请求。"""
    gateway = build_gateway()

    response = await gateway.chat(TaskType.TRIAGE, _MESSAGES, _Sample)

    assert response.error is None
    assert isinstance(response.parsed, _Sample)
    assert response.parsed.count == 0
    assert response.parsed.name


async def test_schema_optional_returns_text() -> None:
    """`schema` 省略时不要求结构化输出，`parsed` 为 `None` 而 `content` 有值。"""
    gateway, _, _, _, _, _ = _build("p1")

    response = await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert response.parsed is None
    assert response.content == "ok"


# --------------------------------------------------- 二、候选链与熔断


async def test_first_provider_failure_falls_over_to_next() -> None:
    """链上首个供应商失败时自动尝试下一个，返回备选并标记 `is_fallback`。"""
    gateway, _, instances, _, _, _ = _build(
        "p1",
        "p2",
        responses={"p1": [_fail(error="http_500", provider="p1")], "p2": [_ok(provider="p2")]},
    )

    response = await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert len(instances["p1"].calls) == 1
    assert len(instances["p2"].calls) == 1
    assert response.provider == "p2"
    assert response.is_fallback is True


async def test_open_circuit_provider_is_skipped_without_call() -> None:
    """熔断中的供应商被跳过，不发起调用，直接走下一个候选。"""
    gateway, _, instances, circuit, _, _ = _build("p1", "p2", threshold=1)
    circuit.record_failure("p1")
    assert circuit.state_of("p1") is CircuitState.OPEN

    response = await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert instances["p1"].calls == []
    assert len(instances["p2"].calls) == 1
    assert response.provider == "p2"


async def test_all_providers_failed_raises() -> None:
    """候选链全部失败时抛 `AllProvidersFailedError`，带上最后一条失败原因。"""
    gateway, _, _, _, _, _ = _build(
        "p1",
        "p2",
        responses={
            "p1": [_fail(error="http_500", provider="p1")],
            "p2": [_fail(error="http_503", provider="p2")],
        },
    )

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert excinfo.value.task_type == TaskType.TRIAGE.value
    assert excinfo.value.last_error == "http_503"


async def test_unavailable_provider_continues_chain() -> None:
    """适配器抛 `ModelUnavailableError`（未配置/禁用）时按失败处理，继续下一跳。"""
    gateway, _, instances, _, _, _ = _build(
        "p1",
        "p2",
        responses={"p1": [ModelUnavailableError("密钥缺失")], "p2": [_ok(provider="p2")]},
    )

    response = await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert len(instances["p1"].calls) == 1
    assert response.provider == "p2"


async def test_failed_attempts_counted_in_circuit() -> None:
    """失败尝试计入熔断器：连续失败达阈值即熔断该供应商。"""
    gateway, _, _, circuit, _, _ = _build(
        "p1",
        "p2",
        responses={
            "p1": [_fail(error="http_500", provider="p1")],
            "p2": [_fail(error="http_500", provider="p2")],
        },
        threshold=1,
    )

    with pytest.raises(AllProvidersFailedError):
        await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert circuit.state_of("p1") is CircuitState.OPEN
    assert circuit.state_of("p2") is CircuitState.OPEN


async def test_meter_records_success_once() -> None:
    """成功调用只写一条计量，携带真实候选与 `is_fallback`。"""
    gateway, _, _, _, meter, _ = _build(
        "p1",
        "p2",
        responses={"p1": [_fail(error="http_500", provider="p1")], "p2": [_ok(provider="p2")]},
    )

    await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert len(meter.calls) == 1
    _, _, candidate, is_fallback = meter.calls[0]
    assert candidate.provider == "p2"
    assert is_fallback is True


async def test_meter_records_final_failure_once() -> None:
    """候选链最终失败只写一条计量，中间失败不写：失败响应没有可信 token 数。"""
    gateway, _, _, _, meter, _ = _build(
        "p1",
        "p2",
        responses={
            "p1": [_fail(error="http_500", provider="p1")],
            "p2": [_fail(error="http_503", provider="p2")],
        },
    )

    with pytest.raises(AllProvidersFailedError):
        await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert len(meter.calls) == 1
    _, response, candidate, _ = meter.calls[0]
    assert candidate.provider == "p2"
    assert response.error == "http_503"


async def test_parse_error_does_not_trip_circuit() -> None:
    """解析失败不计入熔断：它是输出质量问题，不是供应商可用性问题。"""
    gateway, _, _, circuit, _, _ = _build(
        "p1",
        responses={"p1": [_fail(error="parse_error", provider="p1")]},
        threshold=1,
    )

    with pytest.raises(AllProvidersFailedError):
        await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    assert circuit.state_of("p1") is CircuitState.CLOSED


# --------------------------------------------------- 三、上下文裁剪与审计


async def test_history_trimmed_before_dispatch_and_system_kept() -> None:
    """超过 N 轮时裁剪后才发给适配器，`system` 消息保留，且不改动入参。"""
    gateway, _, instances, _, _, _ = _build("p1", max_rounds=6)
    messages = [{"role": "system", "content": "SYS"}] + [{"role": "user", "content": f"u{i}"} for i in range(8)]

    await gateway.chat(TaskType.TRIAGE, messages)

    sent = instances["p1"].calls[0]["messages"]
    assert len(sent) == 8  # system + 摘要 + 最近 6 轮
    assert sent[0] == {"role": "system", "content": "SYS"}
    assert sent[1]["role"] == "system"
    assert len(messages) == 9, "入参不得被修改"


async def test_auditor_receives_input_and_output_digest() -> None:
    """审计模块收到输入与输出的哈希，各一条。"""
    gateway, _, _, _, _, auditor = _build("p1")

    await gateway.chat(TaskType.TRIAGE, _MESSAGES)

    records = auditor.records()
    assert len(records) == 2
    assert len(records[0]["input_digest"]) == 64
    assert records[0]["task_type"] == str(TaskType.TRIAGE)
    assert len(records[1]["output_digest"]) == 64


async def test_audit_records_contain_no_raw_text() -> None:
    """审计记录只留哈希，不含原文——农事内容属敏感数据。"""
    gateway, _, _, _, _, auditor = _build("p1")
    messages = [{"role": "user", "content": "SECRET-TOKEN-42"}]

    await gateway.chat(TaskType.TRIAGE, messages)

    dump = json.dumps(auditor.records(), ensure_ascii=False)
    assert "SECRET-TOKEN-42" not in dump
    assert "叶子" not in dump


def test_trim_messages_is_pure_function() -> None:
    """`trim_messages` 是纯函数：同一输入返回相等结果，且不修改入参。"""
    messages = [{"role": "system", "content": "S"}] + [{"role": "user", "content": f"u{i}"} for i in range(10)]
    snapshot = json.loads(json.dumps(messages))

    first = trim_messages(messages, 6)
    second = trim_messages(messages, 6)

    assert first == second
    assert messages == snapshot


def test_trim_messages_keeps_system_and_summarizes_head() -> None:
    """裁剪保留 `system`，更早的非 system 消息压缩为一条摘要。"""
    messages = [{"role": "system", "content": "S"}] + [{"role": "user", "content": f"u{i}"} for i in range(9)]

    trimmed = trim_messages(messages, 6)

    assert trimmed[0] == {"role": "system", "content": "S"}
    assert trimmed[1]["role"] == "system"
    assert "u0" in trimmed[1]["content"]
    assert len(trimmed) == 8


def test_trim_messages_under_limit_returns_equivalent() -> None:
    """未超轮次时不裁剪，返回与输入相等的内容。"""
    messages = [{"role": "user", "content": f"u{i}"} for i in range(6)]

    assert trim_messages(messages, 6) == messages


def test_record_digest_stable_and_64_hex() -> None:
    """输入相同哈希相同、不同则不同，且为 64 位十六进制。"""
    auditor = CallAuditor()
    payload = [{"role": "user", "content": "abc"}]

    first = auditor.record_digest(TaskType.TRIAGE, payload)
    again = auditor.record_digest(TaskType.TRIAGE, payload)
    other = auditor.record_digest(TaskType.TRIAGE, [{"role": "user", "content": "xyz"}])

    assert first == again
    assert first != other
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)
    assert first == hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# --------------------------------------------------- 四、边界与分层


def test_gateway_does_not_import_business_modules() -> None:
    """L3：网关模块不得导入 `app.agent` / `app.gate` / `app.services` / `app.broadcast`。"""
    source = pathlib.Path(__file__).resolve().parents[2] / "app" / "llm" / "gateway.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = ("app.agent", "app.gate", "app.services", "app.broadcast")
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.append(node.module)

    assert not [imp for imp in imported if any(imp == f or imp.startswith(f + ".") for f in forbidden)]


async def test_mock_mode_makes_no_network_request(respx_mock: respx.Router) -> None:
    """`USE_MOCK_LLM=true` 下全程零外部网络请求。"""
    gateway = build_gateway()

    await gateway.chat(TaskType.TRIAGE, _MESSAGES, _Sample)

    assert respx_mock.calls == []


def test_non_mock_without_adapters_raises(monkeypatch) -> None:
    """`USE_MOCK_LLM=false` 且未装配真实适配器时显式报错，不静默回落到 Mock。"""
    monkeypatch.setenv("USE_MOCK_LLM", "false")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            build_gateway()
    finally:
        get_settings.cache_clear()
