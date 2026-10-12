"""T6-06：14.4 要求写入 trace 的字段是否全部落地。

不跑真实问诊（那会牵动模型与数据库），而是直接驱动 `traced_node` 与 `record_generation`：
属性来源就是节点的返回增量与 generation 的显式参数，本文件把"节点返回什么 → span 上出现
什么键"钉死，避免七个工厂各写一遍导致的字段口径分叉。

分组与技术方案 14.4 的字段清单一一对应：每节点三属性、问诊、证据、闸门、generation。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.observability import (
    GENERATION_SPAN_NAME,
    configure_tracing,
    force_flush,
    generation_span,
    record_generation,
    reset_tracing,
    traced_node,
)
from app.observability.attributes import (
    CONSULTATION_ID,
    EVIDENCE_REF_IDS,
    EVIDENCE_REF_IDS_COUNT,
    EVIDENCE_REF_IDS_LIMIT,
    EVIDENCE_SOURCE_TYPES,
    GATE_DECISION,
    GATE_RULES_HIT,
    INTERVIEW_QUESTION_IDS,
    INTERVIEW_SLOTS_HIT,
    LLM_DEGRADED,
    LLM_LATENCY_MS,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TOKENS,
    NODE_OK,
    NODE_ROUND,
    PROCESS_ID,
)
from app.observability.tracing import spans_of


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "OTEL_ENABLED": True,
        "OTEL_EXPORTER": "console",
        "OTEL_ENDPOINT": "",
        "OTEL_SERVICE_NAME": "huanong-test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    configure_tracing(_settings(), exporter=exp)
    yield exp
    reset_tracing()


async def _run_node(name: str, state: dict, returns: dict) -> None:
    """跑一个返回固定增量的节点，结束后强制刷出 span。"""

    async def _node(_state: dict) -> dict:
        return returns

    await traced_node(name, _node)(state)
    force_flush()


def _one(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in spans_of(exporter) if s.name == name]
    assert matches, f"未找到 span {name}"
    return matches[0]


# ---- 每节点三属性 ----


async def test_every_node_writes_identity_attributes(exporter) -> None:
    """`consultation.id` / `process.id` / `node.round` 每节点必写。"""
    state = {"session_id": "sess-9", "process_id": "rice", "budget": {"interview_rounds": 3}}
    await _run_node("observe", state, {"stage": "observe"})

    span = _one(exporter, "observe")
    assert span.attributes[CONSULTATION_ID] == "sess-9"
    assert span.attributes[PROCESS_ID] == "rice"
    assert span.attributes[NODE_ROUND] == 3
    assert span.attributes[NODE_OK] is True


async def test_round_falls_back_to_legacy_key_and_zero(exporter) -> None:
    """轮次优先取 `budget["interview_rounds"]`，缺失时回退 `round`，都没有给 0。"""
    await _run_node("a", {"session_id": "s", "process_id": "rice", "round": 5}, {})
    assert _one(exporter, "a").attributes[NODE_ROUND] == 5

    await _run_node("b", {"session_id": "s"}, {})
    assert _one(exporter, "b").attributes[NODE_ROUND] == 0


async def test_process_id_unknown_before_triage(exporter) -> None:
    """`intake` 之后、`triage` 之前 `process_id` 为 `None`，属性值用 `"unknown"` 兜底。"""
    await _run_node("intake", {"session_id": "s"}, {"stage": "intake"})
    assert _one(exporter, "intake").attributes[PROCESS_ID] == "unknown"


# ---- 问诊节点 ----


async def test_interview_attributes(exporter) -> None:
    """`interview.slots_hit` 取已填槽位键，`interview.question_ids` 取本轮问题键。"""
    returns = {
        "slots": {"pest": {"status": "filled"}, "area": {"status": "missing"}},
        "pending_questions": [
            {"slot_key": "crop_stage"},
            {"slotKey": "last_use"},
        ],
    }
    await _run_node("interview", {"session_id": "s", "process_id": "rice"}, returns)

    span = _one(exporter, "interview")
    assert span.attributes[INTERVIEW_SLOTS_HIT] == "pest"
    assert span.attributes[INTERVIEW_QUESTION_IDS] == "crop_stage,last_use"


# ---- 证据核查节点 ----


async def test_evidence_attributes(exporter) -> None:
    """`evidence.ref_ids` / `evidence.source_types` 从核查结果取；来源类型取 `region_scope`。"""
    returns = {
        "evidence": [
            {"knowledge_id": "K-1", "region_scope": "national"},
            {"knowledge_id": "K-2", "region_scope": "province"},
        ]
    }
    await _run_node("verify", {"session_id": "s", "process_id": "rice"}, returns)

    span = _one(exporter, "verify")
    assert span.attributes[EVIDENCE_REF_IDS] == "K-1,K-2"
    assert span.attributes[EVIDENCE_REF_IDS_COUNT] == 2
    assert span.attributes[EVIDENCE_SOURCE_TYPES] == "national,province"


async def test_evidence_ref_ids_truncated_with_true_count(exporter) -> None:
    """超过截断阈值时 `evidence.ref_ids` 被截断，`evidence.ref_ids_count` 等于真实总数。"""
    total = EVIDENCE_REF_IDS_LIMIT + 7
    returns = {"evidence": [{"knowledge_id": f"K-{i}", "region_scope": "national"} for i in range(total)]}
    await _run_node("verify", {"session_id": "s", "process_id": "rice"}, returns)

    span = _one(exporter, "verify")
    written = str(span.attributes[EVIDENCE_REF_IDS]).split(",")
    assert len(written) == EVIDENCE_REF_IDS_LIMIT
    assert written[0] == "K-0"
    assert span.attributes[EVIDENCE_REF_IDS_COUNT] == total


# ---- 闸门节点 ----


async def test_gate_attributes(exporter) -> None:
    """`gate.decision` 与 `gate.rules_hit` 从 `gate_result` 取，规则名取 `violations[].rule_id`。"""
    returns = {
        "gate_result": {
            "decision": "block",
            "violations": [{"rule_id": "R-WHITELIST"}, {"rule_id": "R-INTERVAL"}],
        }
    }
    await _run_node("gate_check", {"session_id": "s", "process_id": "rice"}, returns)

    span = _one(exporter, "gate_check")
    assert span.attributes[GATE_DECISION] == "block"
    assert span.attributes[GATE_RULES_HIT] == "R-WHITELIST,R-INTERVAL"


# ---- generation span ----


async def test_generation_attributes(exporter) -> None:
    """`llm.model` / `provider` / `tokens`（输入输出合计）/ `latency_ms` / `degraded`。"""
    with generation_span("diagnosis") as span:
        record_generation(
            span,
            model="deepseek-v4-flash",
            provider="openai_compat",
            prompt_tokens=120,
            completion_tokens=80,
            latency_ms=940,
            degraded=True,
        )
    force_flush()

    gen = _one(exporter, GENERATION_SPAN_NAME)
    assert gen.attributes[LLM_MODEL] == "deepseek-v4-flash"
    assert gen.attributes[LLM_PROVIDER] == "openai_compat"
    assert gen.attributes[LLM_TOKENS] == 200
    assert gen.attributes[LLM_LATENCY_MS] == 940
    assert gen.attributes[LLM_DEGRADED] is True


async def test_generation_span_name_is_fixed(exporter) -> None:
    """generation span 名固定为 `generation`（14.4），不吃节点名。"""
    with generation_span("whatever"):
        pass
    force_flush()

    assert _one(exporter, GENERATION_SPAN_NAME).name == "generation"


async def test_failure_span_status_is_error(exporter) -> None:
    """失败节点：`node.ok=False` 且状态 ERROR（与 `test_tracing.py` 的异常用例互为印证）。"""

    async def _boom(_state: dict) -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await traced_node("boom_node", _boom)({"session_id": "s", "process_id": "rice"})
    force_flush()

    span = _one(exporter, "boom_node")
    assert span.attributes[NODE_OK] is False
    assert span.status.status_code is StatusCode.ERROR
