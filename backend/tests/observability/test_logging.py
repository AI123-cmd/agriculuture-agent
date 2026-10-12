"""T6-08：结构化日志的输出形态、trace 绑定与脱敏。

被测的是**处理器链**，不是某条业务日志。用 `capsys` 抓取 stdout：structlog 的
`PrintLoggerFactory` 写到标准输出，JSON 渲染后的每一行都能 `json.loads`，从而直接断言
字段与"无明文泄漏"。

三类用例：

1. **形态**：`LOG_FORMAT=json` 时每行可解析，含 `level`/`timestamp`/`event`；中文不转义。
2. **绑定**：问诊入口绑定 `consultation_id` 后全链路可见；并发两个会话不串号；trace 在
   span 内时自动带 `trace_id`。
3. **脱敏**：手机号/设备号不出现在输出里；异常堆栈保留且其中的手机号被清洗；
   处理器顺序错了嵌套字段会漏（反证顺序约束有效）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.observability import configure_tracing, consultation_span, reset_tracing
from app.observability import logging as logging_module
from app.observability.logging import bind_context, bound_context, clear_context, configure_logging, log
from app.observability.masker import ObservabilityMasker


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "ENV": "production",
        "LOG_LEVEL": "DEBUG",
        "LOG_FORMAT": "json",
        "MASK_EXTRA_FIELDS": (),
        "MASK_GEO": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def json_logs(capsys: pytest.CaptureFixture[str]):
    """装配 JSON 日志链并清理上下文；返回一个"读已输出行"的取用函数。"""
    configure_logging(_settings())
    clear_context()
    try:

        def read() -> list[dict[str, Any]]:
            out = capsys.readouterr().out
            return [json.loads(line) for line in out.splitlines() if line.strip()]

        # 清掉装配阶段可能产生的输出。
        capsys.readouterr()
        yield read
    finally:
        clear_context()


# ---- 1. 形态 ----

def test_json_output_has_required_fields(json_logs) -> None:
    """每条输出可被 `json.loads` 解析，含 level / timestamp / event。"""
    log.info("consultation.started", process_id="rice")
    rows = json_logs()

    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "consultation.started"
    assert row["level"] == "info"
    assert isinstance(row["timestamp"], str) and row["timestamp"]
    assert row["process_id"] == "rice"


def test_json_output_keeps_chinese_untranslated(json_logs) -> None:
    """`ensure_ascii=False`：中文原样输出，不出现 `\\uXXXX` 转义。"""
    log.info("问诊开始", 环节="判定")
    out = json_logs()
    assert out[0]["event"] == "问诊开始"
    assert out[0]["环节"] == "判定"


def test_console_format_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    """`LOG_FORMAT=console`：输出不是 JSON（本地可读）。"""
    configure_logging(_settings(LOG_FORMAT="console"))
    log.info("consultation.started")
    out = capsys.readouterr().out
    assert out.strip()
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_resolved_mask_geo_follows_env() -> None:
    """坐标脱敏按 ENV 派生：生产开启、非生产关闭；显式配置优先。"""
    assert logging_module._resolve_mask_geo(_settings(ENV="production")) is True
    assert logging_module._resolve_mask_geo(_settings(ENV="development")) is False
    assert logging_module._resolve_mask_geo(_settings(ENV="production", MASK_GEO=False)) is False


# ---- 2. 上下文绑定 ----

def test_bound_context_is_visible_to_logs(json_logs) -> None:
    """`bound_context` 绑定的字段出现在其作用域内的每条日志里。"""
    with bound_context(consultation_id="sess-1"):
        log.info("interview.asked")
        log.info("interview.answered")
    rows = json_logs()
    assert {r["consultation_id"] for r in rows} == {"sess-1"}


def test_bound_context_is_restored_after_exit(json_logs) -> None:
    """退出作用域后字段消失，且不误伤外层绑定。"""
    with bound_context(request_id="req-1"):
        with bound_context(consultation_id="sess-1"):
            log.info("inner")
        log.info("outer")
    rows = json_logs()
    assert rows[0]["consultation_id"] == "sess-1"
    assert rows[0]["request_id"] == "req-1"
    assert "consultation_id" not in rows[1]
    assert rows[1]["request_id"] == "req-1"


def test_trace_context_is_injected_inside_span(capsys: pytest.CaptureFixture[str]) -> None:
    """在 span 内写的日志自动带 `trace_id`/`span_id`（与 T6-06 对账）；span 外则没有。"""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    configure_logging(_settings())
    clear_context()
    configure_tracing(_settings(OTEL_ENABLED=True, OTEL_EXPORTER="console"), exporter=InMemorySpanExporter())
    try:
        with consultation_span("sess-1", "rice"):
            log.info("node.entered")
        log.info("outside.span")
    finally:
        reset_tracing()
        clear_context()

    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    inside = next(r for r in rows if r["event"] == "node.entered")
    outside = next(r for r in rows if r["event"] == "outside.span")
    assert len(inside["trace_id"]) == 32
    assert len(inside["span_id"]) == 16
    assert "trace_id" not in outside


async def test_concurrent_consultations_do_not_cross_contaminate(capsys: pytest.CaptureFixture[str]) -> None:
    """并发两个会话的日志不串号——`contextvars` 绑定的经典缺陷点。"""
    configure_logging(_settings())
    clear_context()
    try:
        async def _run(session_id: str) -> None:
            with bound_context(consultation_id=session_id):
                for _ in range(5):
                    log.info("step", session=session_id)
                    await asyncio.sleep(0)  # 强制交错

        await asyncio.gather(_run("sess-A"), _run("sess-B"))
        out = capsys.readouterr().out
    finally:
        clear_context()

    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(rows) == 10
    for row in rows:
        assert row["consultation_id"] == row["session"]


# ---- 3. 脱敏 ----

def test_phone_and_device_sn_never_leak(json_logs) -> None:
    """含手机号与设备号的日志：输出里不出现两个明文子串（任何形式的泄漏）。"""
    log.info("device.bound", phone="13800000000", device_sn="AB12CD345678")
    out = json_logs()
    assert out[0]["phone"] == "138****0000"
    assert out[0]["device_sn"] == "AB12CD******"


def test_phone_in_message_text_is_masked(json_logs) -> None:
    """手机号出现在消息文本（非字段）里同样被清洗。"""
    log.info("联系 13800000000 未接通")
    out = json_logs()
    assert "13800000000" not in out[0]["event"]
    assert "138****0000" in out[0]["event"]


def test_exception_stack_is_preserved_and_masked(json_logs) -> None:
    """异常堆栈保留，且堆栈里夹带的手机号被清洗。"""
    try:
        raise ValueError("农户 13800000000 的手机号非法")
    except ValueError:
        log.exception("node.failed")
    out = json_logs()
    row = out[0]
    assert row["event"] == "node.failed"
    assert "exception" in row and "Traceback" in row["exception"]
    assert "13800000000" not in row["exception"]
    assert "138****0000" in row["exception"]


def test_nested_sensitive_field_is_masked(json_logs) -> None:
    """嵌套字段被递归脱敏（`prescription.facts_provenance.phone`）。"""
    log.info("prescription.built", prescription={"facts_provenance": {"phone": "13800000000"}})
    out = json_logs()
    assert out[0]["prescription"]["facts_provenance"]["phone"] == "138****0000"


def test_masker_after_renderer_leaks_nested_field(capsys: pytest.CaptureFixture[str]) -> None:
    """反证处理器顺序约束：脱敏排在渲染器之后时，嵌套字段会漏。

    这不是被测代码的行为，而是"顺序断言的用例确实有效"的证明——若把顺序写反，上面的
    `test_nested_sensitive_field_is_masked` 会失败。
    """
    import structlog

    masker = ObservabilityMasker()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(ensure_ascii=False),
            lambda _l, _m, ed: masker.mask(ed),  # 渲染之后才脱敏：只对已成型字符串生效
        ],
        wrapper_class=structlog.make_filtering_bound_logger(10),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    logger = structlog.get_logger("order-probe")
    logger.info("prescription.built", prescription={"facts_provenance": {"phone": "13800000000"}})
    out = capsys.readouterr().out

    # 渲染器先跑，脱敏拿到的是字符串，取不到内层字典 → 明文泄漏。
    assert "13800000000" in out

    # 复位为正常链，避免污染后续用例。
    configure_logging(_settings())
