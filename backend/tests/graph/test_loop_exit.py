"""T6-04：两类回环都必须有出口（轮次上限，不用超时断言）。

问诊回环若轮次没有被正确递增，图会无限循环直到超时；输出测试只会以「超时」失败，报错信息
无法定位是哪条条件边。本文件改用**路径断言**：喂入「永远填不满槽位」的输入，断言
`traced.count("interview")` 有上界且路径终点不是 `prescribe`；不使用
`pytest.raises(TimeoutError)` 这类依赖超时的断言。

两类回环：

1. **问诊回环**（`route_after_interview`）：槽位未齐备且轮次未达 `max_interview_rounds` 时
   回到 `interview`；用尽即出口（子图 `handoff` → `__end__`）。
2. **判定回环**（`route_after_judge`）：置信不足且还有可问，回 `interview` 补问；`judge_loops`
   达上限即出口。

用最小可复现图驱动（`interview` / `judge` 配条件边与内存检查点），把断言精确落在回环计数上，
不牵扯分诊、现场采集与模型的抖动。替身节点真实读写 `budget`，因此计数逻辑是被测对象本身。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.budget import ConsultBudget
from app.agent.graph.routes import ROUTE_AFTER_INTERVIEW, ROUTE_AFTER_JUDGE, route_after_interview, route_after_judge
from app.agent.state import ConsultStage, ConsultationState
from tests.graph.conftest import memory_checkpointer, run_path

_MAX_INTERVIEW = ConsultBudget().max_interview_rounds
_MAX_JUDGE_LOOPS = ConsultBudget().max_judge_loops

# 无会诊流程的问诊路由：剔除 `panel` 分支（本文件不注册 panel 节点）。
_INTERVIEW_ROUTES = {key: target for key, target in ROUTE_AFTER_INTERVIEW.items() if key != "panel"}


def _budget(**overrides: Any) -> dict:
    budget = ConsultBudget()
    for key, value in overrides.items():
        setattr(budget, key, value)
    return budget.to_state()


def _state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-loop",
        "redlines_loaded": True,
        "required_slots": ["symptom"],
        "slots": {},
        "budget": _budget(),
    }
    base.update(overrides)
    return base


# ---- 问诊回环 ----


def _interview_loop_app(always_missing: bool):
    """最小问诊图：interview → route_after_interview → interview / verify / __end__。"""

    calls = {"n": 0}

    async def interview(state: ConsultationState) -> dict:
        calls["n"] += 1
        budget = dict(state.get("budget") or {})
        budget["interview_rounds"] = budget.get("interview_rounds", 0) + 1
        if always_missing or calls["n"] == 1:
            return {"stage": ConsultStage.INTERVIEW, "budget": budget, "slots": {}}
        # 第二轮起补齐槽位，让路由进入 verify，避免真无限循环
        return {
            "stage": ConsultStage.INTERVIEW,
            "budget": budget,
            "slots": {"symptom": {"key": "symptom", "value": "叶片发黄", "status": "filled", "source": "user_answer"}},
        }

    async def verify(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.VERIFY}

    graph = StateGraph(ConsultationState)
    graph.add_node("interview", interview)
    graph.add_node("verify", verify)
    graph.add_edge(START, "interview")
    graph.add_conditional_edges("interview", route_after_interview, _INTERVIEW_ROUTES)
    graph.add_edge("verify", END)
    app = graph.compile(checkpointer=memory_checkpointer())
    return app, calls


async def test_interview_loop_has_bounded_exit() -> None:
    """永远填不满槽位：interview 次数不超过 `max_interview_rounds`，且不走到 prescribe。"""
    app, calls = _interview_loop_app(always_missing=True)

    traced = await run_path(app, _state(), thread_id="t-loop-interview")

    assert calls["n"] <= _MAX_INTERVIEW, f"问诊回环没有出口：interview 执行了 {calls['n']} 次"
    assert traced.count("interview") == _MAX_INTERVIEW
    assert "verify" not in traced  # 未收敛出口：直接出子图（__end__），不进 verify
    assert "prescribe" not in traced


async def test_interview_loop_continues_when_budget_remains() -> None:
    """反向对照：第一轮未齐备 → 回环一次 → 第二轮补齐进 verify，证明回环本身可用。"""
    app, calls = _interview_loop_app(always_missing=False)

    traced = await run_path(app, _state(), thread_id="t-loop-interview-ok")

    assert calls["n"] == 2
    assert traced.count("interview") == 2
    assert "verify" in traced


# ---- 判定回环 ----


def _judge_loop_app():
    """最小判定图：judge → route_after_judge → interview / prescribe / __end__；interview → judge。"""

    calls = {"judge": 0, "interview": 0}

    async def judge(state: ConsultationState) -> dict:
        calls["judge"] += 1
        budget = dict(state.get("budget") or {})
        budget["judge_loops"] = budget.get("judge_loops", 0) + 1
        return {
            "stage": ConsultStage.JUDGE,
            "budget": budget,
            "diagnosis": {
                "out_of_scope": False,
                "confidence_level": "uncertain",
                "needs_more_info": ["叶片背面"],
            },
        }

    async def interview(state: ConsultationState) -> dict:
        calls["interview"] += 1
        return {"stage": ConsultStage.INTERVIEW}

    async def prescribe(state: ConsultationState) -> dict:
        return {"stage": ConsultStage.PRESCRIBE}

    graph = StateGraph(ConsultationState)
    graph.add_node("judge", judge)
    graph.add_node("interview", interview)
    graph.add_node("prescribe", prescribe)
    graph.add_edge(START, "judge")
    graph.add_conditional_edges("judge", route_after_judge, ROUTE_AFTER_JUDGE)
    graph.add_edge("interview", "judge")
    graph.add_edge("prescribe", END)
    app = graph.compile(checkpointer=memory_checkpointer())
    return app, calls


async def test_judge_loop_has_bounded_exit() -> None:
    """置信一直不足：`judge` 次数被 `max_judge_loops` 限住，最终出口而非无限补问。"""
    app, calls = _judge_loop_app()

    traced = await run_path(app, _state(), thread_id="t-loop-judge")

    assert calls["judge"] <= _MAX_JUDGE_LOOPS + 1, f"判定回环没有出口：judge 执行了 {calls['judge']} 次"
    assert calls["interview"] <= _MAX_JUDGE_LOOPS
    assert "prescribe" not in traced  # 未收敛出口：不产出处方
