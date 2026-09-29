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
from app.agent.contracts import InterviewBatch, InterviewQuestion
from app.agent.graph import factories
from app.agent.state import ConsultationState, ProcessId
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
