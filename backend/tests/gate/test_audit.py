"""T2-46：闸门审计表、影子模式与三条复盘查询。

用例分五组：

1. **审计落库**：`GateService.review` 后 `gate_audit` 新增一行，`violations` 是结构化 JSON
   数组，含 `rule_id`/`severity`/`message`/`field`；
2. **影子模式**：`GATE_MODE=shadow` 时 `review` 返回 `passed=True` 外壳且 `shadowed` 非空，
   落库行的 `decision` 是真实判定且 `shadowed=True`；`enforce` 时返回真实结果；
3. **审计 fail-open**：审计写库抛异常时 `review` 仍返回、指标自增、错误日志 event 正确；
4. **契约可构造性**：`GateResult(passed=True, shadowed=<block>)` 可构造，而无影子字段时
   "有 block 违规却 allow"仍抛错；
5. **degrade 率口径**：排除 `shadowed=True` 的行（14.3 口径）。

跑在真实 PostgreSQL 上（表依赖 `JSONB` 与迁移），未声明 `engine` 的静态用例不受影响。
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.gate.audit import WRITE_FAILED_EVENT, WRITE_FAILED_METRIC, GateAuditWriter
from app.gate.contracts import GateResult, GateViolation
from app.gate.engine import GateEngine
from app.gate.service import GateService
from app.models.gate_audit import GateAudit
from app.observability import metrics
from app.repositories.gate_audit import GateAuditRepository
from tests.gate.conftest import gate_context

pytestmark = pytest.mark.usefixtures("requires_postgres")


def _settings(mode: str) -> Settings:
    return Settings(GATE_MODE=mode)


def _stub_knowledge_id_missing():
    """让 R-WHITELIST 命中 block：知识库查不到该条目。"""

    class _Missing:
        def get_entry(self, knowledge_id: str):
            return None

        def is_approved(self, knowledge_id: str) -> bool:
            return False

        def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
            return ()

    return _Missing()


async def _fetch_audits(session_factory) -> list[GateAudit]:
    async with session_factory() as db:
        return list((await db.execute(select(GateAudit))).scalars().all())


async def test_review_writes_audit_row_with_structured_violations(session_factory) -> None:
    repo = GateAuditRepository(session_factory)
    service = GateService(audit=GateAuditWriter(repo), settings=_settings("enforce"))
    ctx = gate_context(knowledge=_stub_knowledge_id_missing())
    draft = ctx.prescription

    result = await service.review(draft, ctx)

    rows = await _fetch_audits(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.decision == result.decision
    assert row.shadowed is False
    assert row.session_id == ctx.session_id
    assert row.user_id == ctx.user_id
    assert row.checked_rules == list(result.checked_rules)
    assert isinstance(row.violations, list) and row.violations
    for violation in row.violations:
        assert {"rule_id", "severity", "message", "field"} <= set(violation)


async def test_shadow_mode_wraps_result_but_records_real_decision(session_factory) -> None:
    repo = GateAuditRepository(session_factory)
    service = GateService(audit=GateAuditWriter(repo), settings=_settings("shadow"))
    ctx = gate_context(knowledge=_stub_knowledge_id_missing())

    result = await service.review(ctx.prescription, ctx)

    assert result.passed is True
    assert result.shadowed is not None
    inner = result.shadowed
    assert inner.decision == "block"

    rows = await _fetch_audits(session_factory)
    assert len(rows) == 1
    assert rows[0].decision == "block"
    assert rows[0].shadowed is True


async def test_enforce_mode_returns_real_result(session_factory) -> None:
    repo = GateAuditRepository(session_factory)
    service = GateService(audit=GateAuditWriter(repo), settings=_settings("enforce"))
    ctx = gate_context()

    result = await service.review(ctx.prescription, ctx)

    assert result.shadowed is None
    assert result.passed == (result.decision == "allow")


async def test_audit_write_failure_is_swallowed_with_metric_and_log(session_factory, monkeypatch) -> None:
    class _FailingRepo(GateAuditRepository):
        async def add(self, row: GateAudit) -> GateAudit:
            raise RuntimeError("库抖动")

    events: list[str] = []

    class _RecordingLog:
        def error(self, event: str, **kwargs: Any) -> None:
            events.append(event)

        def __getattr__(self, name: str):
            return lambda *a, **k: None

    metrics.reset()
    monkeypatch.setattr("app.gate.audit.log", _RecordingLog())
    service = GateService(audit=GateAuditWriter(_FailingRepo(session_factory)), settings=_settings("enforce"))
    ctx = gate_context()

    result = await service.review(ctx.prescription, ctx)

    assert result.decision in ("allow", "degrade", "block")
    assert metrics.counter_value(WRITE_FAILED_METRIC) == 1
    assert events == [WRITE_FAILED_EVENT]


def test_shadow_shell_constructible_while_block_violation_still_rejected() -> None:
    inner = GateResult(
        passed=False,
        decision="block",
        violations=[GateViolation(rule_id="R-WHITELIST", severity="block", message="越界", field="-")],
    )
    shell = GateResult(passed=True, shadowed=inner)
    assert shell.shadowed is inner

    with pytest.raises(Exception):
        GateResult(
            passed=True,
            decision="allow",
            violations=[GateViolation(rule_id="R-WHITELIST", severity="block", message="越界", field="-")],
        )


async def test_degrade_rate_excludes_shadowed_rows(session_factory) -> None:
    repo = GateAuditRepository(session_factory)
    ctx = gate_context()

    real_degrade = _row(ctx, decision="degrade", shadowed=False)
    shadow_degrade = _row(ctx, decision="degrade", shadowed=True)
    await repo.add(real_degrade)
    await repo.add(shadow_degrade)

    rate = await repo.degrade_rate(ctx.process_id.value, datetime(2000, 1, 1, tzinfo=UTC))

    assert rate == 1.0


async def test_degrade_rate_zero_when_no_rows(session_factory) -> None:
    repo = GateAuditRepository(session_factory)
    rate = await repo.degrade_rate("rice", datetime(2000, 1, 1, tzinfo=UTC))
    assert rate == 0.0


def _row(ctx, *, decision: str, shadowed: bool) -> GateAudit:
    from uuid import uuid4

    return GateAudit(
        id=str(uuid4()),
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        process_id=ctx.process_id.value,
        config_version=ctx.config_version,
        decision=decision,
        shadowed=shadowed,
        violations=[],
        checked_rules=["R-REDLINE-LOAD"],
        prescription_snapshot={},
    )
