"""按供应商独立的熔断器（T2-58）。

按供应商独立是关键：主供应商 A 故障不应让主供应商 B 也被熔断，否则降级链失去意义。
熔断状态以供应商名为键，`record_failure` 只影响对应供应商。

三态的意义：`CLOSED` 正常；连续失败达阈值转 `OPEN`，在冷却期内直接拒绝请求（不打
无效调用）；冷却结束后转 `HALF_OPEN`，放行少量探针；探针连续成功则回 `CLOSED`，失败
则回 `OPEN` 重新冷却。`HALF_OPEN` 的探针要有数量上限，否则供应商刚恢复时会被大量并发
请求同时打过去，可能再次击垮它。

阈值与冷却时长全部由构造注入（`app/core/config.py`），源码不含硬编码魔数。时钟也由
构造注入：冷却测试若依赖真实等待，用例会变慢且不稳定。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(str, Enum):
    """熔断状态。"""

    CLOSED = "closed"  # 正常放行
    OPEN = "open"  # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 半开，放行少量探针请求


@dataclass
class _ProviderState:
    """单个供应商的熔断状态。"""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float = 0.0
    probes_used: int = 0
    half_open_successes: int = 0
    changed_at: float = field(default=0.0)


class CircuitBreaker:
    """按供应商独立的熔断器。三态状态机。"""

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: int,
        half_open_probes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._half_open_probes = half_open_probes
        self._clock = clock
        self._states: dict[str, _ProviderState] = {}

    # ---- 查询 ----

    def state_of(self, provider: str) -> CircuitState:
        """读当前状态（会顺带处理"冷却期已过"的自动转半开）。"""
        return self._state_of(provider).state

    def allow(self, provider: str) -> bool:
        """是否放行一次调用。OPEN 且未到冷却期返回 False。"""
        st = self._state_of(provider)
        if st.state is CircuitState.CLOSED:
            return True
        if st.state is CircuitState.OPEN:
            if not self._cooled_down(st):
                return False
            # 冷却结束：转入 HALF_OPEN。这一次放行本身就是探针，必须计入探针预算，
            # 否则实际放行数是 1 + half_open_probes，比配置多一个并发请求。
            self._set_state(provider, st, CircuitState.HALF_OPEN)
        # HALF_OPEN：只放行有限个探针，用完即拒（不再放行，等待结果回写状态）
        return self._half_open_budget(st)

    # ---- 记账 ----

    def record_failure(self, provider: str) -> None:
        """记录一次失败。CLOSED 达到阈值转 OPEN，HALF_OPEN 直接回到 OPEN。"""
        st = self._state_of(provider)
        st.consecutive_failures += 1
        st.consecutive_successes = 0
        if st.state is CircuitState.HALF_OPEN:
            self._set_state(provider, st, CircuitState.OPEN)
            return
        if st.consecutive_failures >= self._failure_threshold:
            self._set_state(provider, st, CircuitState.OPEN)

    def record_success(self, provider: str) -> None:
        """记录成功。HALF_OPEN 连续成功转回 CLOSED。"""
        st = self._state_of(provider)
        st.consecutive_failures = 0
        st.consecutive_successes += 1
        if st.state is CircuitState.HALF_OPEN:
            st.half_open_successes += 1
            if st.half_open_successes >= self._half_open_probes:
                self._set_state(provider, st, CircuitState.CLOSED)
            return
        if st.state is CircuitState.OPEN:
            # OPEN 期间的"成功"只可能来自冷却后的探针（此时应已转 HALF_OPEN），
            # 这里保守地按恢复处理，避免状态机卡死在 OPEN。
            self._set_state(provider, st, CircuitState.HALF_OPEN)

    # ---- 内部 ----

    def _state_of(self, provider: str) -> _ProviderState:
        st = self._states.get(provider)
        if st is None:
            st = _ProviderState(changed_at=self._clock())
            self._states[provider] = st
        return st

    def _cooled_down(self, st: _ProviderState) -> bool:
        return (self._clock() - st.opened_at) >= self._cooldown_seconds

    def _half_open_budget(self, st: _ProviderState) -> bool:
        if st.probes_used >= self._half_open_probes:
            return False
        st.probes_used += 1
        return True

    def _set_state(self, provider: str, st: _ProviderState, state: CircuitState) -> None:
        now = self._clock()
        if st.state is CircuitState.OPEN and state is CircuitState.OPEN:
            # 重新冷却：更新开启时刻，否则会被误判为"冷却期已过"而立刻放行探针。
            st.opened_at = now
        elif state is CircuitState.OPEN:
            st.opened_at = now
        if state is CircuitState.HALF_OPEN:
            st.probes_used = 0
            st.half_open_successes = 0
        st.state = state
        st.changed_at = now
        self._states[provider] = st


__all__ = ["CircuitBreaker", "CircuitState"]
