"""T2-29：油菜与通用农业两条流程子图的占位断言。

两条流程都不是一期完整交付对象：

- **油菜**按产品说明书 5.4 与附录 A7 以占位方式实现，三条相互独立的约束同时生效——
  `RAPE-01` 红线（闸门 `R-REDLINE` 拦截）、L3 检索开关 `disabled`（`pesticide_items` 恒空）、
  输出定位为鉴别与农事；
- **通用农业**是分诊降级的兜底定位，不建农事历（`calendar_source = null`）、无专属阈值与
  药剂条目、不启用并行会诊。

本文件沿用 T2-27 的做法：用**确定性的叶子替身**替换会调模型或查库的通用节点，保留
`rapeseed.py` / `general.py` 真实的接线代码，从而断言这两条子图与一期五条在控制流上完全
同构（含 `report_guard`，且都不注册 `panel`）。与「零药剂」「不建农事历」强相关的判定落到
真实的闸门引擎与流程配置上，而不是替身。
"""

from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from app.agent.agents.base import AgentContext
from app.agent.agents.prescriber import PrescriberAgent
from app.agent.agents.triage import TriageAgent
from app.agent.contracts import EvidenceItem, EvidenceQuery
from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.graph.subgraphs import general as general_module
from app.agent.graph.subgraphs import rapeseed as rapeseed_module
from app.agent.state import ConsultStage, ConsultationState, ProcessId, TaskType
from app.gate.contracts import GateContext, GatePayload, GatePrescription
from app.gate.engine import GateEngine
from app.llm import ModelGateway, build_gateway
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.redlines import LoadedRedlines, RedlineValidator

# 注册表在本项补全后应恰好七条（技术方案 7.4 的七选一分发）。
_EXPECTED_FLOWS = ["chili", "general", "pig", "rapeseed", "rice", "tea", "weed"]

# 走查核对：子图源文件不得出现硬编码的药剂名、剂量或安全间隔期字面量。
_FORBIDDEN_LITERALS = ("亩", "克", "毫升", "间隔期")

# 无会诊流程的顶层节点顺序：observe → report_guard → interview → verify → judge → prescribe。
_EXPECTED_TRACE = ["observe", "report_guard", "interview", "verify", "judge", "prescribe"]


# ---- 叶子替身（与 T2-27 同构，只保留真实接线） ----


def _observe() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.OBSERVE}

    return _node


def _guard() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"report_decision": {"triggered": False, "stop_consultation": False}}

    return _node


def _interview() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.INTERVIEW, "panel_needed": False, "slots": {}}

    return _node


def _verify() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.VERIFY}

    return _node


def _judge() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.JUDGE, "diagnosis": {"confidence_level": "likely", "out_of_scope": False}}

    return _node


def _prescribe() -> Callable[[ConsultationState], Awaitable[dict]]:
    async def _node(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.PRESCRIBE}

    return _node


def _patch_leaf_factories(monkeypatch, module) -> None:
    """替换子图模块里的叶子工厂，保留子图文件真实的接线代码。"""
    monkeypatch.setattr(module, "make_observe_node", lambda config: _observe())
    monkeypatch.setattr(module, "build_report_guard_node", lambda config: _guard())
    monkeypatch.setattr(module, "make_interview_node", lambda config: _interview())
    monkeypatch.setattr(module, "make_verify_node", lambda config: _verify())
    monkeypatch.setattr(module, "make_judge_node", lambda config: _judge())
    monkeypatch.setattr(module, "make_prescribe_node", lambda config: _prescribe())


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-t2-29",
        "redlines_loaded": True,
        "required_slots": [],
        "slots": {},
        "config_version": 1,
        "messages": [],
    }
    base.update(overrides)
    return base


async def _trace(graph, state: dict) -> list[str]:
    names: list[str] = []
    async for chunk in graph.astream(state, stream_mode="updates"):
        names.extend(chunk.keys())
    return names


# ---- 1. 注册表恰为七条 ----


def test_registry_has_exactly_seven_flows() -> None:
    """油菜与通用补全后，注册表恰为七条流程，不多不少。"""
    assert sorted(p.value for p in SUBGRAPH_BUILDERS) == _EXPECTED_FLOWS


def test_rapeseed_and_general_builders_registered() -> None:
    assert SUBGRAPH_BUILDERS[ProcessId.RAPESEED] is rapeseed_module.build_rapeseed_subgraph
    assert SUBGRAPH_BUILDERS[ProcessId.GENERAL] is general_module.build_general_subgraph


# ---- 2. 两条子图与一期同构：含 report_guard、不注册 panel ----


async def test_rapeseed_subgraph_is_structurally_isomorphic(monkeypatch) -> None:
    """油菜：与一期非会诊流程同构，且不注册 `panel`（`panel_enabled=false`）。"""
    _patch_leaf_factories(monkeypatch, rapeseed_module)
    compiled = rapeseed_module.build_rapeseed_subgraph()

    trace = await _trace(compiled, _state(process_id=ProcessId.RAPESEED))

    assert trace == _EXPECTED_TRACE
    assert "report_guard" in set(compiled.get_graph().nodes)
    assert "panel" not in set(compiled.get_graph().nodes)


async def test_general_subgraph_is_structurally_isomorphic(monkeypatch) -> None:
    """通用：与一期非会诊流程同构，且不注册 `panel`。"""
    _patch_leaf_factories(monkeypatch, general_module)
    compiled = general_module.build_general_subgraph()

    trace = await _trace(compiled, _state(process_id=ProcessId.GENERAL))

    assert trace == _EXPECTED_TRACE
    assert "report_guard" in set(compiled.get_graph().nodes)
    assert "panel" not in set(compiled.get_graph().nodes)


def test_both_flows_disable_panel_in_config() -> None:
    """`panel_enabled=false` 来自配置，不是代码分支：两条流程的 YAML 都声明为假。"""
    assert get_process_config(ProcessId.RAPESEED).panel_enabled is False
    assert get_process_config(ProcessId.GENERAL).panel_enabled is False


# ---- 3. 油菜：零药剂（L3 检索开关 disabled 落在检索层） ----


class _RecordingKnowledge:
    """检索替身：记录调用次数，用于证明油菜流程**不检索**。"""

    def __init__(self) -> None:
        self.calls: list[EvidenceQuery] = []

    async def search(self, query: EvidenceQuery, region_chain: list[str]) -> list[EvidenceItem]:
        self.calls.append(query)
        return []


class _NoRegionMatcher:
    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        return []


async def test_rapeseed_never_searches_pesticides() -> None:
    """油菜流程 `fetch_pesticide_candidates` 直接返回空且不触发检索：药剂恒空的第一层保证。"""
    config = get_process_config(ProcessId.RAPESEED)
    knowledge = _RecordingKnowledge()
    agent = PrescriberAgent(cast(ModelGateway, build_gateway()), knowledge, _NoRegionMatcher())
    ctx = AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.PRESCRIBE,
        trace_id="t-1",
    )

    candidates = await agent.fetch_pesticide_candidates(
        state=cast(ConsultationState, {"messages": [{"role": "user", "content": "叶片出现水渍状病斑，要不要打药"}]}),
        ctx=ctx,
    )

    assert candidates == []
    assert knowledge.calls == []


def test_rapeseed_declares_rape01_redline() -> None:
    """`RAPE-01` 必须在配置中声明且可被校验器加载（药剂恒空的第二层保证）。"""
    config = get_process_config(ProcessId.RAPESEED)

    loaded = RedlineValidator().validate(config)

    assert loaded.loaded is True
    assert {r.id for r in loaded.redlines} >= {"RAPE-01"}


# ---- 4. 油菜：闸门确以 R-REDLINE 拦截（process_redlines_loaded=True） ----


class _FakeKnowledge:
    """`KnowledgeFacts` 最小实现：无药剂条目，供空处方的正向核对。"""

    def get_entry(self, knowledge_id: str):
        return None

    def is_approved(self, knowledge_id: str) -> bool:
        return False

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ()


def _rapeseed_gate_context(rendered_text: str) -> GateContext:
    config = get_process_config(ProcessId.RAPESEED)
    loaded: LoadedRedlines = RedlineValidator().validate(config)
    return GateContext(
        session_id="sess-t2-29-r",
        user_id="user-1",
        process_id=ProcessId.RAPESEED,
        config_version=config.version,
        prescription=GatePrescription(
            diagnosis_summary="油菜茎秆水渍状病斑，疑为菌核病",
            actions=[],
            pesticide_items=[],
        ),
        payload=GatePayload(rendered_text=rendered_text),
        redlines=loaded,
        slots={},
        knowledge=_FakeKnowledge(),
        today=date(2025, 5, 1),
    )


def test_rapeseed_gate_checks_redline_rule_without_output() -> None:
    """未涉用药的油菜输出：放行，且 `R-REDLINE` 确实执行过（红线已加载，非同"缺失"）。"""
    ctx = _rapeseed_gate_context("油菜茎秆出现水渍状病斑，建议先清沟排水并通风，观察菌核病的发展，必要时联系当地农技站确认。")

    result = GateEngine().evaluate(ctx)

    assert result.decision == "allow"
    assert result.process_redlines_loaded is True
    assert "R-REDLINE" in result.checked_rules


def test_rapeseed_prescription_has_no_numeric_paths() -> None:
    """空处方的数值路径为空：没有药剂就不会出现需要溯源的药剂数值。"""
    ctx = _rapeseed_gate_context("油菜茎秆出现水渍状病斑，建议先清沟排水并通风。")

    assert ctx.prescription.pesticide_items == []
    assert ctx.prescription.numeric_paths() == []


# ---- 5. 通用：不建农事历 ----


def test_general_flow_has_no_calendar_source() -> None:
    """通用流程 `calendar_source is None`：提示快照链路（T3-03）据此不建农事历。"""
    config = get_process_config(ProcessId.GENERAL)

    assert config.calendar_source is None
    assert config.display_name == "通用农业"


async def test_general_subgraph_has_no_calendar_node(monkeypatch) -> None:
    """通用子图不含任何农事历相关节点：`calendar_source=None` 时不触发任何查询。"""
    _patch_leaf_factories(monkeypatch, general_module)
    compiled = general_module.build_general_subgraph()

    node_names = {str(name) for name in compiled.get_graph().nodes}
    assert not any("calendar" in name.lower() or "农事历" in name for name in node_names)


# ---- 6. 分诊降级到通用流程的可观察性 ----


async def test_triage_degrade_reason_names_general_flow() -> None:
    """`fallback_reason` 含"通用农业"：接口层据此在 reply 说明当前按通用建议作答。"""
    configs = load_all_configs()
    agent = TriageAgent(cast(ModelGateway, build_gateway()), configs)
    ctx = AgentContext(
        process_config=get_process_config(ProcessId.GENERAL),
        config_version=1,
        task_type=TaskType.TRIAGE,
        trace_id="t-1",
    )

    result = await agent.degrade(cast(ConsultationState, {"profile": {}}), ctx, "网关不可用")

    assert result.process_id is ProcessId.GENERAL
    assert result.fallback_reason
    assert "通用农业" in result.fallback_reason


# ---- 7. 走查核对：子图无硬编码药剂/剂量/间隔期字面量 ----


@pytest.mark.parametrize("module", [rapeseed_module, general_module])
def test_subgraph_source_has_no_hardcoded_prescription_literals(module) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")
    for literal in _FORBIDDEN_LITERALS:
        assert literal not in source, f"{module.__name__} 不应出现硬编码字面量「{literal}」"
