"""T2-59 验收：token 计量、成本估算与看板聚合。

用例分三段：

1. **纯计算**——`estimate_cost_micros` 的输入输出分别计价、未配置单价记 0；
   `pricing_from_settings` 容忍非法条目；`micros_to_yuan` 的换算。
2. **落库与记录形态**——成功写一条且 token/耗时取自 `ModelResponse`、链路中间失败不写、
   最终失败只写一条 `ok=False`、`is_fallback` 与 `is_degraded` 分别记录、`input_digest`
   为 64 位哈希且不留原文、记录带 `process_id` 与 `agent`、写库失败不回灌影响 `chat` 返回。
3. **聚合**——`cost_by_agent` 按成本降序且 token 为输入输出之和、`degrade_ratio` 给出两个
   不同的比值、聚合 SQL 带 `created_at BETWEEN` 与 `GROUP BY`、仓储不出现 `db.query(`。

第 2 段的"落库"用假会话工厂捕获 `session.add` 的行，不需要数据库；第 3 段的聚合跑在真实
PostgreSQL 上（表由迁移建出，顺带验证"迁移与 ORM 一致"）。
"""

import ast
import hashlib
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from app.llm import TaskType
from app.llm.gateway import CallAuditor, ModelGateway
from app.llm.metering import (
    CallContext,
    ModelPricing,
    NullTokenMeter,
    TokenMeterImpl,
    pricing_from_settings,
)
from app.llm.providers.base import ModelResponse
from app.llm.router import ModelCandidate, RoutingDecision
from app.llm.tiers import ModelTier
from app.models.llm_call import LlmCall
from app.llm.circuit import CircuitBreaker
from app.repositories.llm_call import (
    LlmCallRepository,
    cost_statement,
    degrade_statement,
)
from app.services.cost_dashboard_service import micros_to_yuan

_PRICING = {"m1": ModelPricing(input_micros_per_k=1000, output_micros_per_k=1000)}


# --------------------------------------------------- 替身


class _FakeSession:
    """最小异步会话：只实现计量用到的 `add` / `commit` / 上下文协议。"""

    def __init__(self, sink: list[LlmCall], *, fail_commit: bool) -> None:
        self._sink = sink
        self._fail_commit = fail_commit

    def add(self, row: LlmCall) -> None:
        self._sink.append(row)

    async def commit(self) -> None:
        if self._fail_commit:
            raise RuntimeError("模拟数据库不可用")

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSessionFactory:
    """捕获被写入的行；`fail_commit=True` 时提交抛异常，用于验证"计量写失败不阻断"。"""

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.rows: list[LlmCall] = []
        self._fail_commit = fail_commit

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.rows, fail_commit=self._fail_commit)


class _ScriptedProvider:
    def __init__(self, response: ModelResponse) -> None:
        self.name = response.provider
        self._response = response
        self.calls = 0

    async def chat(
        self, model: str, messages: list[dict], response_schema: object = None, timeout: float = 30.0
    ) -> ModelResponse:
        self.calls += 1
        return self._response.model_copy()


class _StaticRouter:
    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision

    def resolve(self, task_type: object, *, budget_remaining: int | None = None) -> RoutingDecision:
        return self._decision


def _ok(*, provider: str = "p1", model: str = "m1") -> ModelResponse:
    return ModelResponse(
        content="ok",
        parsed=None,
        model=model,
        provider=provider,
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=42,
    )


def _fail(*, provider: str = "p1", model: str = "m1") -> ModelResponse:
    return ModelResponse(
        content=None,
        parsed=None,
        model=model,
        provider=provider,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        error="http_500",
    )


def _candidate(provider: str = "p1", *, degraded: bool = False) -> ModelCandidate:
    return ModelCandidate(provider=provider, model="m1", tier=ModelTier.PRIMARY, is_degraded=degraded)


def _decision(*providers: str, degraded_last: bool = False) -> RoutingDecision:
    chain = tuple(_candidate(p, degraded=degraded_last and i == len(providers) - 1) for i, p in enumerate(providers))
    return RoutingDecision(
        task_type=TaskType.DIAGNOSE,
        tier=ModelTier.PRIMARY,
        chain=chain,
        max_tokens=2048,
        max_history_rounds=6,
    )


def _gateway(decision: RoutingDecision, providers: dict[str, object], meter: object) -> ModelGateway:
    return ModelGateway(
        router=_StaticRouter(decision),
        providers=providers,
        circuit=CircuitBreaker(failure_threshold=5, cooldown_seconds=30, half_open_probes=2, clock=lambda: 0.0),
        meter=meter,
        auditor=CallAuditor(),
    )


# --------------------------------------------------- 一、纯计算


def test_estimate_cost_prices_input_and_output_separately() -> None:
    """输入 1000、输出 1000、单价各 1000 微元/千 → 2000。"""
    meter = TokenMeterImpl(_FakeSessionFactory(), pricing=_PRICING)  # type: ignore[arg-type]
    assert meter.estimate_cost_micros("m1", 1000, 1000) == 2000


def test_estimate_cost_differs_when_output_price_higher() -> None:
    """输出单价远高于输入时，成本不再等于简单的 token 之和（输入输出分开计价）。"""
    pricing = {"m2": ModelPricing(input_micros_per_k=1000, output_micros_per_k=3000)}
    meter = TokenMeterImpl(_FakeSessionFactory(), pricing=pricing)  # type: ignore[arg-type]
    assert meter.estimate_cost_micros("m2", 1000, 1000) == 4000
    # 同样 2000 token，若按"输入单价"一律计价只是 2000，分开计价才能反映真实成本。
    assert meter.estimate_cost_micros("m2", 1000, 1000) != meter.estimate_cost_micros("m2", 2000, 0)


def test_estimate_cost_returns_zero_for_unpriced_model() -> None:
    meter = TokenMeterImpl(_FakeSessionFactory(), pricing=_PRICING)  # type: ignore[arg-type]
    assert meter.estimate_cost_micros("未知模型", 1000, 1000) == 0


def test_pricing_from_settings_skips_invalid_entries() -> None:
    """非法条目跳过而非报错：缺一条只影响该模型成本记 0，不应让进程起不来。"""
    pricing = pricing_from_settings({"m1": [1000, 2000], "bad": [1], "worse": "x"})
    assert set(pricing) == {"m1"}
    assert pricing["m1"] == ModelPricing(input_micros_per_k=1000, output_micros_per_k=2000)


def test_micros_to_yuan_conversion() -> None:
    assert micros_to_yuan(2_500_000) == 2.5
    assert micros_to_yuan(0) == 0.0


async def test_null_meter_is_a_noop() -> None:
    """`NullTokenMeter` 不落库、不报错，供无库场景装配。"""
    await NullTokenMeter().record(TaskType.TRIAGE, _ok(), _candidate(), is_fallback=False, is_degraded=False)


# --------------------------------------------------- 二、落库形态


async def test_success_writes_one_row_with_tokens_and_latency_from_response() -> None:
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]

    await meter.record(TaskType.DIAGNOSE, _ok(), _candidate(), is_fallback=False, is_degraded=False)

    assert len(factory.rows) == 1
    row = factory.rows[0]
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 50
    assert row.latency_ms == 42
    assert row.ok is True
    assert row.error_code is None
    # token 100 + 50、单价各 1000/千 → 100*1 + 50*1 = 150 微元
    assert row.cost_micros == 150


async def test_record_carries_agent_process_and_consultation() -> None:
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]
    ctx = CallContext(
        agent="diagnoser",
        user_id="u-1",
        consultation_id="c-9",
        process_id="rice",
        model_version="v2026-09",
    )

    await meter.record(TaskType.DIAGNOSE, _ok(), _candidate(), is_fallback=False, is_degraded=False, ctx=ctx)

    row = factory.rows[0]
    assert row.agent == "diagnoser"
    assert row.process_id == "rice"
    assert row.consultation_id == "c-9"
    assert row.user_id == "u-1"
    assert row.model_version == "v2026-09"


async def test_record_without_context_falls_back_to_task_type() -> None:
    """无上下文时 `agent` 退化为任务类型名，保证非空列总有值。"""
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]

    await meter.record(TaskType.OBSERVE, _ok(), _candidate(), is_fallback=False, is_degraded=False)

    row = factory.rows[0]
    assert row.agent == "observe"
    assert row.task_type == "observe"
    assert row.user_id is None and row.process_id is None


async def test_fallback_and_degraded_recorded_separately() -> None:
    """`is_fallback`（服务故障走备选）与 `is_degraded`（预算降档用私有化）分别落到两列。"""
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]

    await meter.record(TaskType.DIAGNOSE, _ok(), _candidate(degraded=True), is_fallback=True, is_degraded=True)

    row = factory.rows[0]
    assert row.is_fallback is True
    assert row.is_degraded is True


async def test_input_digest_is_hex_hash_and_keeps_no_raw_text() -> None:
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]
    raw = "叶片背面有灰色霉层，最近连续降雨"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    await meter.record(
        TaskType.DIAGNOSE, _ok(), _candidate(), is_fallback=False, is_degraded=False, input_digest=digest
    )

    row = factory.rows[0]
    assert row.input_digest == digest
    assert len(row.input_digest) == 64
    assert raw not in " ".join(
        str(getattr(row, c)) for c in ("agent", "task_type", "model", "input_digest", "error_code")
    )


async def test_unpriced_model_records_tokens_but_zero_cost() -> None:
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing={})  # type: ignore[arg-type]

    await meter.record(TaskType.DIAGNOSE, _ok(), _candidate(), is_fallback=False, is_degraded=False)

    row = factory.rows[0]
    assert row.cost_micros == 0
    assert row.prompt_tokens == 100 and row.completion_tokens == 50


async def test_metering_write_failure_does_not_break_chat() -> None:
    """计量写失败只记日志，`chat` 仍正常返回成功响应。"""
    factory = _FakeSessionFactory(fail_commit=True)
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]
    decision = _decision("p1")
    gateway = _gateway(decision, {"p1": _ScriptedProvider(_ok(provider="p1"))}, meter)

    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}])

    assert response.error is None
    # 行确实构造了，只是提交失败被吞掉
    assert len(factory.rows) == 1


async def test_intermediate_failure_writes_nothing_until_success() -> None:
    """链上首个失败不计费、不写行；由第二个候选成功，只落一条 `ok=True`。"""
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]
    decision = _decision("p1", "p2")
    gateway = _gateway(
        decision,
        {"p1": _ScriptedProvider(_fail(provider="p1")), "p2": _ScriptedProvider(_ok(provider="p2"))},
        meter,
    )

    response = await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}])

    assert response.provider == "p2" and response.is_fallback is True
    assert len(factory.rows) == 1
    row = factory.rows[0]
    assert row.ok is True
    assert row.provider == "p2"
    assert row.is_fallback is True


async def test_final_failure_writes_exactly_one_failed_row() -> None:
    """候选链全部失败：只补一条 `ok=False`，token 记 0，错误码为最后一次尝试。"""
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]
    decision = _decision("p1", "p2")
    gateway = _gateway(
        decision,
        {"p1": _ScriptedProvider(_fail(provider="p1")), "p2": _ScriptedProvider(_fail(provider="p2"))},
        meter,
    )

    from app.llm.errors import AllProvidersFailedError

    with pytest.raises(AllProvidersFailedError):
        await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}])

    assert len(factory.rows) == 1
    row = factory.rows[0]
    assert row.ok is False
    assert row.error_code == "http_500"
    assert row.prompt_tokens == 0 and row.completion_tokens == 0


async def test_degraded_candidate_is_recorded_as_degraded_on_success() -> None:
    """成功走的是链尾私有化候选时，记录里 `is_degraded` 为真。"""
    factory = _FakeSessionFactory()
    meter = TokenMeterImpl(factory, pricing=_PRICING)  # type: ignore[arg-type]
    decision = _decision("private", degraded_last=True)
    gateway = _gateway(decision, {"private": _ScriptedProvider(_ok(provider="private"))}, meter)

    await gateway.chat(TaskType.DIAGNOSE, [{"role": "user", "content": "x"}])

    assert factory.rows[0].is_degraded is True


# --------------------------------------------------- 三、聚合（真实 PostgreSQL）

_SINCE = datetime(2026, 9, 1, tzinfo=UTC)
_UNTIL = datetime(2026, 9, 30, tzinfo=UTC)


def _row(**overrides: object) -> LlmCall:
    base: dict[str, object] = {
        "agent": "diagnoser",
        "task_type": "diagnose",
        "tier": "primary",
        "provider": "p1",
        "model": "m1",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_micros": 150,
        "latency_ms": 10,
        "is_fallback": False,
        "is_degraded": False,
        "ok": True,
        "created_at": _SINCE + timedelta(days=1),
    }
    base.update(overrides)
    return LlmCall(**base)


async def _seed(session_factory, rows: list[LlmCall]) -> None:
    async with session_factory() as db:
        db.add_all(rows)
        await db.commit()


async def test_cost_by_agent_is_cost_descending_with_token_sum(session_factory) -> None:
    await _seed(
        session_factory,
        [
            _row(agent="a", cost_micros=100, prompt_tokens=10, completion_tokens=5),
            _row(agent="b", cost_micros=300, prompt_tokens=20, completion_tokens=30),
            _row(agent="b", cost_micros=50, prompt_tokens=1, completion_tokens=1),
        ],
    )

    async with session_factory() as db:
        rows = await LlmCallRepository(db).cost_by_agent(_SINCE, _UNTIL)

    assert [r.agent for r in rows] == ["b", "a"]
    by_agent = {r.agent: r for r in rows}
    assert by_agent["b"].cost_micros == 350
    assert by_agent["b"].calls == 2
    # token 为输入输出之和
    assert by_agent["b"].tokens == 20 + 30 + 1 + 1
    assert by_agent["a"].tokens == 10 + 5


async def test_cost_by_process_and_task_type_grouped(session_factory) -> None:
    await _seed(
        session_factory,
        [
            _row(process_id="rice", task_type="diagnose", cost_micros=100),
            _row(process_id="rice", task_type="prescribe", cost_micros=200),
            _row(process_id="rape", task_type="diagnose", cost_micros=400),
        ],
    )

    async with session_factory() as db:
        repo = LlmCallRepository(db)
        by_process = await repo.cost_by_process(_SINCE, _UNTIL)
        by_task = await repo.cost_by_task_type(_SINCE, _UNTIL)

    assert [r.process_id for r in by_process] == ["rape", "rice"]
    assert {r.process_id: r.cost_micros for r in by_process}["rice"] == 300
    assert {r.task_type: r.cost_micros for r in by_task}["diagnose"] == 500


async def test_degrade_ratio_returns_two_distinct_ratios(session_factory) -> None:
    """降级率与降档率分别计算：构造 2 条 fallback、1 条 degraded、共 4 条。"""
    await _seed(
        session_factory,
        [
            _row(is_fallback=True),
            _row(is_fallback=True),
            _row(is_degraded=True),
            _row(),
        ],
    )

    async with session_factory() as db:
        ratio = await LlmCallRepository(db).degrade_ratio(_SINCE, _UNTIL)

    assert ratio.total == 4
    assert ratio.fallback_count == 2 and ratio.degraded_count == 1
    assert ratio.fallback_ratio == pytest.approx(0.5)
    assert ratio.degraded_ratio == pytest.approx(0.25)
    assert ratio.fallback_ratio != ratio.degraded_ratio


async def test_degrade_ratio_is_zero_without_calls(session_factory) -> None:
    async with session_factory() as db:
        ratio = await LlmCallRepository(db).degrade_ratio(_SINCE, _UNTIL)
    assert ratio.total == 0
    assert ratio.fallback_ratio == 0.0 and ratio.degraded_ratio == 0.0


async def test_sum_tokens_for_consultation(session_factory) -> None:
    """预算守卫按 `consultation_id` 汇总 token。"""
    await _seed(
        session_factory,
        [
            _row(consultation_id="c-1", prompt_tokens=100, completion_tokens=50),
            _row(consultation_id="c-1", prompt_tokens=10, completion_tokens=5),
            _row(consultation_id="c-2", prompt_tokens=999, completion_tokens=999),
        ],
    )

    async with session_factory() as db:
        repo = LlmCallRepository(db)
        assert await repo.sum_tokens_for_consultation("c-1") == 165
        assert await repo.sum_tokens_for_consultation("不存在") == 0


async def test_window_excludes_rows_outside_range(session_factory) -> None:
    await _seed(
        session_factory,
        [
            _row(created_at=_SINCE - timedelta(days=1)),
            _row(created_at=_UNTIL + timedelta(days=1)),
            _row(created_at=_SINCE + timedelta(days=5)),
        ],
    )

    async with session_factory() as db:
        rows = await LlmCallRepository(db).cost_by_agent(_SINCE, _UNTIL)

    assert len(rows) == 1 and rows[0].calls == 1


async def test_dashboard_snapshot_aggregates_all_dimensions(session_factory) -> None:
    from app.services.cost_dashboard_service import CostDashboardService

    await _seed(session_factory, [_row(agent="diagnoser", cost_micros=150, process_id="rice")])

    async with session_factory() as db:
        snapshot = await CostDashboardService(db).snapshot(since=_SINCE, until=_UNTIL)

    assert snapshot.total_cost_micros == 150
    assert snapshot.total_cost_yuan == pytest.approx(0.00015)
    assert [r.agent for r in snapshot.by_agent] == ["diagnoser"]
    assert snapshot.degrade.total == 1


# --------------------------------------------------- 四、SQL 与分层约束


def test_cost_statement_sql_has_between_and_group_by() -> None:
    sql = str(cost_statement(LlmCall.agent, _SINCE, _UNTIL)).upper()
    assert "BETWEEN" in sql
    assert "GROUP BY" in sql
    assert "LLM_CALL.AGENT" in sql


def test_degrade_statement_sql_has_between() -> None:
    sql = str(degrade_statement(_SINCE, _UNTIL)).upper()
    assert "BETWEEN" in sql
    assert "IS_FALLBACK" in sql and "IS_DEGRADED" in sql


def test_repository_does_not_call_db_query() -> None:
    """L4：仓储不得出现 `db.query(`，聚合一律走 `select()`。"""
    tree = ast.parse(pathlib.Path("app/repositories/llm_call.py").read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "query"
        and isinstance(node.func.value, ast.Name)
    ]
    assert offenders == []
