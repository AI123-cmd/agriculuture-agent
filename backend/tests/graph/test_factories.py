"""T2-26：节点工厂的「config 决定行为」与缓存/幂等。

工厂函数的职责是把**配置注入通用节点**，差异全部来自 `ProcessConfig`。因此本文件盯住三件
与具体智能体行为无关的性质：

1. `get_config` 走加载器的进程内缓存：同一 `ProcessId` 两次调用返回同一对象（`is` 相等），
   配置缺失时抛异常而不是返回 `None`；
2. 七个工厂对同一 config 幂等（节点名确定），对不同流程产出不同的节点名；
3. 工厂闭包确实把对应 config 注入了节点：问诊节点用注入配置算出的候选方向落在该流程的
   `required_slots` 内，且两条流程的候选集合不同。

问诊节点在运行时要 `interrupt`，故第 3 点用**最小可运行图 + 替身问诊智能体**驱动：替身不去
调模型，而是用真实候选池筛选器（`select_candidate_slots`）从注入的 config 算出方向，从而把
"工厂注入了哪份 config"变成可断言的事实。`make_panel_node` 只按 `panel_enabled` 决定返回
节点还是 `None`，不需要跑图。
"""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.agents.candidate_pool import select_candidate_slots
from app.agent.agents.exceptions import AgentDegraded
from app.agent.contracts import DiagnosisResult, InterviewBatch, InterviewQuestion
from app.agent.graph import factories
from app.agent.state import ConsultationState, ConsultStage, ProcessId
from app.process_config.errors import ConfigLoadError

# 两条结构不同的流程：水稻不启用会诊、茶启用；其必问槽位键集也不同。
_RICE = ProcessId.RICE
_TEA = ProcessId.TEA


# ---- get_config：缓存与缺失即抛 ----


def test_get_config_returns_cached_same_object() -> None:
    """同一 `ProcessId` 连续两次返回同一对象（`is` 相等），证明缓存生效、未重复读盘。"""
    first = factories.get_config(_RICE)
    second = factories.get_config(_RICE)

    assert first is second


def test_get_config_distinguishes_processes() -> None:
    """不同流程返回不同配置对象。"""
    assert factories.get_config(_RICE) is not factories.get_config(_TEA)
    assert factories.get_config(_RICE).process_id is _RICE
    assert factories.get_config(_TEA).process_id is _TEA


def test_get_config_propagates_load_error_instead_of_none(monkeypatch) -> None:
    """配置缺失必须抛异常而非返回 `None`：静默兜底会把"流程不可用"变成没人知道的事实。"""

    def _boom(process_id: ProcessId):
        raise ConfigLoadError(f"流程配置缺失: {process_id.value}")

    monkeypatch.setattr(factories, "get_process_config", _boom)

    with pytest.raises(ConfigLoadError):
        factories.get_config(ProcessId.GENERAL)


# ---- panel 工厂：由 panel_enabled 决定有无 ----


def test_panel_node_absent_for_rice() -> None:
    """水稻 `panel_enabled=false`：返回 `None`，子图据此不注册该节点。"""
    assert factories.make_panel_node(factories.get_config(_RICE)) is None


def test_panel_node_present_for_tea() -> None:
    """茶 `panel_enabled=true`：返回可调用节点。"""
    node = factories.make_panel_node(factories.get_config(_TEA))

    assert node is not None
    assert callable(node)
    assert node.__name__ == f"panel_{_TEA.value}"


# ---- 节点名：确定且随流程变化 ----


@pytest.mark.parametrize("kind", ["observe", "interview", "verify", "judge", "prescribe"])
def test_node_names_are_process_scoped(kind: str) -> None:
    """五个通用节点的 `__name__` 带流程后缀：两条流程的节点名不同，同名会被 LangGraph 视为同一节点。"""
    maker = getattr(factories, f"make_{kind}_node")
    rice = maker(factories.get_config(_RICE))
    tea = maker(factories.get_config(_TEA))

    assert rice.__name__ == f"{kind}_{_RICE.value}"
    assert tea.__name__ == f"{kind}_{_TEA.value}"
    assert rice.__name__ != tea.__name__


def test_handoff_node_reuses_shared_builder() -> None:
    """转人工的判定与渲染是通用实现：`make_handoff_node` 直接复用 `build_handoff_node(config)`。"""
    node = factories.make_handoff_node(factories.get_config(_RICE))

    assert callable(node)


def test_node_factory_is_idempotent() -> None:
    """同一 config 两次构造得到确定性的节点名（幂等），不会因调用次序改变行为标识。"""
    config = factories.get_config(_RICE)
    first = factories.make_interview_node(config)
    second = factories.make_interview_node(config)

    assert first.__name__ == second.__name__


def test_no_hardcoded_process_branch_in_factories() -> None:
    """工厂体内不得写死流程分支（`ProcessId.XXX`）：差异必须由 config 表达。"""
    import inspect

    source = inspect.getsource(factories)
    assert "ProcessId." not in source


# ---- 工厂确实注入了对应的 config ----


class _RecordingInterviewer:
    """替身问诊智能体：用真实候选池筛选器从「注入的 config」算出方向，便于断言注入了哪份配置。"""

    def __init__(self) -> None:
        self.seen_process_ids: list[ProcessId] = []

    async def run(self, state: ConsultationState, ctx: Any) -> InterviewBatch:
        self.seen_process_ids.append(ctx.process_config.process_id)
        pool = select_candidate_slots(ctx.process_config, state)
        questions = [
            InterviewQuestion(
                slot_key=candidate.slot_key,
                text=candidate.display_label,
                why="了解情况",
                options=candidate.options,
                answer_type="single" if candidate.options else "text",
            )
            for candidate in pool.candidates
        ]
        return InterviewBatch(questions=questions, stage_hint="正在了解情况")


def _interview_graph(node):
    graph = StateGraph(ConsultationState)
    graph.add_node("interview", node)
    graph.add_edge(START, "interview")
    graph.add_edge("interview", END)
    return graph.compile(checkpointer=InMemorySaver())


async def _run_interview(monkeypatch, process_id: ProcessId) -> tuple[list[str], _RecordingInterviewer]:
    """用指定流程的 config 建问诊节点并跑一次中断，返回被问到的槽位键与替身。"""
    recorder = _RecordingInterviewer()
    monkeypatch.setattr(factories, "InterviewerAgent", lambda *a, **k: recorder)

    node = factories.make_interview_node(factories.get_config(process_id))
    app = _interview_graph(node)
    state = {"session_id": "sess-factories", "slots": {}, "budget": {"interview_rounds": 0}}
    result = await app.ainvoke(state, {"configurable": {"thread_id": f"t-factories-{process_id.value}"}})

    interrupts = result.get("__interrupt__")
    assert interrupts, f"问诊节点应产生中断，实际：{result}"
    asked = [q["slot_key"] for q in interrupts[0].value["questions"]]
    return asked, recorder


async def test_interview_node_injects_matching_config(monkeypatch) -> None:
    """水稻问诊节点注入的是水稻 config，候选方向落在水稻 `required_slots` 内。"""
    asked, recorder = await _run_interview(monkeypatch, _RICE)
    rice_keys = {spec.key for spec in factories.get_config(_RICE).required_slots}

    assert recorder.seen_process_ids == [_RICE]
    assert asked
    assert set(asked) <= rice_keys


async def test_interview_nodes_of_two_processes_differ(monkeypatch) -> None:
    """两条流程的问诊节点产出不同候选槽位集合，各自落在自己的 `required_slots` 内。"""
    rice_asked, _ = await _run_interview(monkeypatch, _RICE)
    tea_asked, _ = await _run_interview(monkeypatch, _TEA)

    rice_keys = {spec.key for spec in factories.get_config(_RICE).required_slots}
    tea_keys = {spec.key for spec in factories.get_config(_TEA).required_slots}

    assert set(rice_asked) <= rice_keys
    assert set(tea_asked) <= tea_keys
    assert set(rice_asked) != set(tea_asked)  # 配置不同 → 候选不同


# ---- 降级出口与预算计数：两处 500/死循环的回归 ----
#
# 问诊降级（`AgentDegraded`）本意是"无题可问，应由主图推进到下一环节"。若节点不接住它，
# 它会一路冒泡成 HTTP 500；判定回环若不计 `judge_loops`，`route_after_judge` 的上限形同
# 虚设、置信一直不足时会无限回环。两处都是「异常路径绕开正常控制流」的同一类缺陷。


class _DegradingInterviewer:
    """替身问诊智能体：直接抛 `AgentDegraded`，模拟候选池与模板池都为空。"""

    async def run(self, state: ConsultationState, ctx: Any) -> InterviewBatch:
        raise AgentDegraded("无可用模板问题，应由主图推进到下一环节")


async def test_interview_node_absorbs_agent_degraded(monkeypatch) -> None:
    """问诊降级不得冒泡成 500：节点接住后推进（无待答问题），并照常记一轮问诊。

    本项的直接现象是农户收到"服务器内部错误"——`AgentDegraded` 逃出节点、穿过图、
    落到 HTTP 层。修复口径是"降级即推进"：返回空 `pending_questions` 让
    `route_after_interview` 按槽位齐备情况继续，而不是把异常抛给调用方。
    """
    monkeypatch.setattr(factories, "InterviewerAgent", lambda *a, **k: _DegradingInterviewer())

    node = factories.make_interview_node(factories.get_config(_RICE))
    state = {"session_id": "sess-degraded", "slots": {}, "budget": {"interview_rounds": 0}}

    out = await node(state)

    assert out["pending_questions"] == []
    assert out["stage"] is ConsultStage.INTERVIEW
    # 异常路径不绕开预算：仍需记一轮问诊，供 `route_after_interview` 的上限收敛。
    assert out["budget"]["interview_rounds"] == 1


class _UncertainDiagnoser:
    """替身判定智能体：返回"不确定 + 待补信息 + 未超边界"，即即将回环的情形。"""

    def __init__(self, result: DiagnosisResult) -> None:
        self._result = result
        self.state: dict[str, Any] = {}

    async def run(self, state: ConsultationState, ctx: Any) -> DiagnosisResult:
        return self._result

    def post_validate(self, state: ConsultationState, result: DiagnosisResult) -> None:
        # 真实智能体会把内部置信度原地写进 state；此处简化为不写分数字段。
        return None


def _uncertain_diagnosis() -> DiagnosisResult:
    return DiagnosisResult(
        conclusion="尚不能确定",
        confidence_level="uncertain",
        basis=["症状描述不足以区分"],
        uncertainty_source=["缺少现场照片"],
        excluded=[],
        terminology_profile="rice",
        needs_more_info=["补拍叶片正面照片"],
        out_of_scope=False,
    )


async def test_judge_node_increments_judge_loops(monkeypatch) -> None:
    """判定要补问时必须记一次回环：否则 `judge_loops` 恒为 0，上限失效、可无限回环。"""
    diagnoser = _UncertainDiagnoser(_uncertain_diagnosis())
    monkeypatch.setattr(factories, "DiagnoserAgent", lambda *a, **k: diagnoser)

    node = factories.make_judge_node(factories.get_config(_RICE))
    state = {"session_id": "sess-loops", "slots": {}, "budget": {"judge_loops": 0, "max_judge_loops": 2}}

    out = await node(state)

    assert out["budget"]["judge_loops"] == 1


async def test_judge_node_skips_loop_count_when_confident(monkeypatch) -> None:
    """正常开方路径不递增回环计数：避免无谓占用预算、提前触发转人工。"""
    result = DiagnosisResult(
        conclusion="很可能是稻瘟病",
        confidence_level="likely",
        basis=["典型病斑形态"],
        uncertainty_source=[],
        excluded=[],
        terminology_profile="rice",
        needs_more_info=[],
        out_of_scope=False,
    )
    monkeypatch.setattr(factories, "DiagnoserAgent", lambda *a, **k: _UncertainDiagnoser(result))

    node = factories.make_judge_node(factories.get_config(_RICE))
    state = {"session_id": "sess-confident", "slots": {}, "budget": {"judge_loops": 0}}

    out = await node(state)

    assert "budget" not in out or out["budget"]["judge_loops"] == 0
