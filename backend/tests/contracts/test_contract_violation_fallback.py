"""T6-05：契约违约触发重试与降级。

技术方案 5.2 与 10.2 的核心策略是"解析失败即契约违约，让模型重出一次，仍失败则降级"，
绝不从自由文本里正则硬抽（农业场景下编造的剂量数字会被农户照着用）。本模块在契约层收口
这条链路：模型吐出的非法枚举值/非法结构必须被基类接住，**不把异常抛到接口层**。

失败行为分两类（全局约定第六节，对应 `FailureMode`）：

- 分诊属 `DEGRADE`：降级后落 `general` 兜底流程，仍然服务；
- 报告守卫属 `FAIL_CLOSED`：不确定时选更保守的一方——"有疑即报"，返回"停止问诊并转人工"。

用例走真实的 `TriageAgent` 与 `ReportGuardAgent`（而不是最小替身子类）：`test_base_agent.py`
已覆盖基类分派逻辑本身，这里要断言的是**具体智能体的降级出口**是否落在正确流程/正确结论上。
全部用替身网关，不连数据库、不发网络请求。
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from app.agent.agents.base import AgentContext
from app.agent.agents.report_guard import ReportGuardAgent
from app.agent.agents.triage import TriageAgent
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.llm.gateway import ModelGateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import load_all_configs


def _illegal_enum_response() -> ModelResponse:
    """模型给出大写流程标识（非法枚举）时的网关响应：`parsed=None` + `error`。"""
    return ModelResponse(
        content=json.dumps({"process_id": "TEA"}, ensure_ascii=False),
        parsed=None,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1,
        error="枚举越界：process_id 不在允许取值内",
    )


class _FakeGateway:
    """回放同一非法响应的网关替身：每次调用都解析失败，逼出降级路径。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, list[dict], object]] = []

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.calls.append((task_type, list(messages), schema))
        return _illegal_enum_response()


class _EmptyDirectory:
    """联系入口占位实现：解析不到时守卫留空并记 ERROR，不编造号码。"""

    def for_region(self, region_code: str | None) -> list[Any]:
        return []


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def triage_ctx() -> AgentContext:
    configs = load_all_configs()
    return AgentContext(
        process_config=configs[ProcessId.GENERAL],
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-fallback",
    )


@pytest.fixture()
def triage_state() -> ConsultationState:
    return {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-fallback",
        "process_id": ProcessId.TEA,
        "round": 1,
        "slot_round": 0,
        "profile": {"object_label": "茶树·2亩"},
        "messages": [{"role": "user", "content": "我家茶树叶子发黄"}],
    }


async def test_triage_illegal_enum_degrades_to_general(
    triage_state: ConsultationState, triage_ctx: AgentContext
) -> None:
    """分诊收到非法枚举值：重试一次仍失败则降级到 `general`，不抛异常到调用方。

    总调用次数为 `max_attempts + 1 = 3`（首次 + 2 次重试），与 T2-13 的基类上限一致。
    """
    gateway = _FakeGateway()
    agent = TriageAgent(cast(ModelGateway, gateway), load_all_configs())

    result = await agent.run(triage_state, triage_ctx)

    assert len(gateway.calls) == 3
    assert result.process_id is ProcessId.GENERAL
    assert result.fallback_reason and result.fallback_reason.strip()
    # 降级只上报一次，标签标明是分诊的 degrade 出口。
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "triage", "mode": "degrade"}) == 1


async def test_triage_degrade_keeps_profile_label(triage_state: ConsultationState, triage_ctx: AgentContext) -> None:
    """降级出口不是"给个空值"：保留档案中的对象标签，供后续流程继续展示。"""
    gateway = _FakeGateway()
    agent = TriageAgent(cast(ModelGateway, gateway), load_all_configs())

    result = await agent.run(triage_state, triage_ctx)

    assert result.object_label == "茶树·2亩"


async def test_report_guard_fail_closed_on_invalid_observation(triage_ctx: AgentContext) -> None:
    """报告守卫收到非法结构（现场采集契约违约）时 fail-closed：停止问诊并转人工。

    守卫不调模型，非法结构来自上游写入 state 的 `observations`。校验失败即视为"拿不到
    可信输入"，此时唯一正确出口是"有疑即报"，而不是放行继续问诊。
    """
    agent = ReportGuardAgent(_EmptyDirectory())
    state: ConsultationState = {
        "session_id": "s2",
        "user_id": "u2",
        "trace_id": "t-guard",
        "process_id": ProcessId.PIG,
        # 缺少必填的 env_source，`ObservationResult.model_validate` 会抛 ValidationError。
        "observations": {"data_available": True},
        "messages": [{"role": "user", "content": "猪突然死了好几头"}],
    }

    result = await agent.run(state, triage_ctx)

    assert result.triggered is True
    assert result.stop_consultation is True
    assert result.matched_signals == ["guard_failure"]
    assert result.mandatory_actions
    # 守卫失败上报一次，标签标明是 fail_closed 出口。
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "report_guard", "mode": "fail_closed"}) == 1


async def test_report_guard_fail_closed_does_not_raise(triage_ctx: AgentContext) -> None:
    """fail-closed 是"返回保守结论"，不是把异常抛到图中断整条链路。"""
    agent = ReportGuardAgent(_EmptyDirectory())
    state: ConsultationState = {
        "session_id": "s3",
        "user_id": "u3",
        "trace_id": "t-guard-2",
        "process_id": ProcessId.PIG,
        "observations": {"env_source": "bogus_source"},
        "messages": [],
    }

    result = await agent.run(state, triage_ctx)

    assert result.triggered is True
    assert result.stop_consultation is True
