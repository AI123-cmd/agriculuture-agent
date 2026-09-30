"""T2-58：按供应商独立的熔断器。

用例覆盖三态状态机的完整迁移，并固定两条关键性质：

1. **状态机**——连续失败达阈值转 `OPEN`、冷却期内持续拒绝、冷却后首次放行进
   `HALF_OPEN`、探针数达上限即拒、探针连续成功回 `CLOSED`、探针一次失败回 `OPEN`。
2. **供应商隔离**——A 的熔断不影响 B 的放行，否则降级链失去意义。

时钟由构造注入（`clock`），所以冷却相关的用例零等待、可复现——依赖真实 `sleep` 的
用例既慢又不稳。阈值与冷却时长同样由构造注入，源码不含硬编码魔数（另有用例扫描源码）。
"""

import ast
import inspect
import pathlib

import pytest

from app.llm.circuit import CircuitBreaker, CircuitState
from app.llm.factory import build_gateway
from app.core.config import get_settings


class _FakeClock:
    """可手动推进的时钟，替代 `time.monotonic` 让冷却可测。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(*, threshold: int = 3, cooldown: int = 30, probes: int = 2) -> tuple[CircuitBreaker, _FakeClock]:
    clock = _FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=threshold,
        cooldown_seconds=cooldown,
        half_open_probes=probes,
        clock=clock,
    )
    return breaker, clock


# ---- 初始态 ----


def test_new_provider_starts_closed() -> None:
    breaker, _ = _breaker()
    assert breaker.state_of("A") is CircuitState.CLOSED
    assert breaker.allow("A") is True


# ---- 阈值与开启 ----


def test_failure_below_threshold_stays_closed() -> None:
    breaker, _ = _breaker(threshold=3)
    breaker.record_failure("A")
    breaker.record_failure("A")
    assert breaker.state_of("A") is CircuitState.CLOSED
    assert breaker.allow("A") is True


def test_failures_reaching_threshold_open_circuit() -> None:
    breaker, _ = _breaker(threshold=3)
    for _ in range(3):
        breaker.record_failure("A")
    assert breaker.state_of("A") is CircuitState.OPEN
    assert breaker.allow("A") is False


def test_open_circuit_keeps_rejecting_within_cooldown() -> None:
    breaker, clock = _breaker(threshold=3, cooldown=30)
    for _ in range(3):
        breaker.record_failure("A")
    clock.advance(10)
    assert breaker.allow("A") is False
    clock.advance(10)
    assert breaker.allow("A") is False


def test_success_resets_failure_counter() -> None:
    """中断的连续失败不应累加到阈值：成功一次即清零。"""
    breaker, _ = _breaker(threshold=3)
    breaker.record_failure("A")
    breaker.record_failure("A")
    breaker.record_success("A")
    breaker.record_failure("A")
    breaker.record_failure("A")
    assert breaker.state_of("A") is CircuitState.CLOSED


# ---- 冷却与半开 ----


def test_after_cooldown_first_allow_enters_half_open() -> None:
    breaker, clock = _breaker(threshold=3, cooldown=30)
    for _ in range(3):
        breaker.record_failure("A")
    clock.advance(30)
    assert breaker.allow("A") is True
    assert breaker.state_of("A") is CircuitState.HALF_OPEN


def test_half_open_allows_at_most_probes_then_rejects() -> None:
    breaker, clock = _breaker(threshold=3, cooldown=30, probes=2)
    for _ in range(3):
        breaker.record_failure("A")
    clock.advance(30)
    assert breaker.allow("A") is True
    assert breaker.allow("A") is True
    assert breaker.allow("A") is False


def test_half_open_consecutive_success_closes_circuit() -> None:
    breaker, clock = _breaker(threshold=3, cooldown=30, probes=2)
    for _ in range(3):
        breaker.record_failure("A")
    clock.advance(30)
    breaker.allow("A")
    breaker.record_success("A")
    breaker.allow("A")
    breaker.record_success("A")
    assert breaker.state_of("A") is CircuitState.CLOSED
    assert breaker.allow("A") is True


def test_half_open_single_failure_reopens_circuit() -> None:
    breaker, clock = _breaker(threshold=3, cooldown=30, probes=3)
    for _ in range(3):
        breaker.record_failure("A")
    clock.advance(30)
    breaker.allow("A")
    breaker.record_success("A")
    breaker.allow("A")
    breaker.record_failure("A")
    assert breaker.state_of("A") is CircuitState.OPEN


def test_reopen_restarts_cooldown() -> None:
    """半开失败重回 OPEN 时必须重置开启时刻，否则会被误判为冷却已过而立刻放行。"""
    breaker, clock = _breaker(threshold=3, cooldown=30, probes=2)
    for _ in range(3):
        breaker.record_failure("A")
    clock.advance(30)
    breaker.allow("A")
    breaker.record_failure("A")
    clock.advance(10)
    assert breaker.allow("A") is False


# ---- 供应商隔离 ----


def test_one_provider_open_does_not_affect_another() -> None:
    breaker, _ = _breaker(threshold=3)
    for _ in range(3):
        breaker.record_failure("A")
    assert breaker.allow("A") is False
    assert breaker.allow("B") is True
    assert breaker.state_of("B") is CircuitState.CLOSED


def test_failures_are_counted_per_provider() -> None:
    breaker, _ = _breaker(threshold=3)
    breaker.record_failure("A")
    breaker.record_failure("B")
    breaker.record_failure("A")
    assert breaker.state_of("A") is CircuitState.CLOSED
    breaker.record_failure("A")
    assert breaker.state_of("A") is CircuitState.OPEN
    assert breaker.state_of("B") is CircuitState.CLOSED


# ---- 与装配/配置的关系 ----


def test_mock_mode_gateway_does_not_trip_circuit() -> None:
    """`USE_MOCK_LLM=true` 下供应商恒可用，正常调用不产生熔断。"""
    settings = get_settings()
    if not settings.USE_MOCK_LLM:
        pytest.skip("当前环境非 mock 模式")
    gateway = build_gateway()
    circuit = gateway._circuit  # noqa: SLF001
    assert circuit.state_of("mock") is CircuitState.CLOSED
    assert circuit.allow("mock") is True


def test_circuit_has_no_hardcoded_thresholds() -> None:
    """阈值/冷却/探针数均由构造注入（技术方案要求"源码无硬编码魔数"）。

    断言方式是"没有默认值"：三个参数都必须在构造时给出，配置只来自 `Settings`，
    模块里也不存在承载阈值的模块级常量。
    """
    import inspect

    params = inspect.signature(CircuitBreaker.__init__).parameters
    for name in ("failure_threshold", "cooldown_seconds", "half_open_probes"):
        assert name in params
        assert params[name].default is inspect.Parameter.empty

    module = ast.parse(pathlib.Path("app/llm/circuit.py").read_text(encoding="utf-8"))
    module_level_ints = [
        node.targets[0].id
        for node in module.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
    ]
    assert module_level_ints == []
