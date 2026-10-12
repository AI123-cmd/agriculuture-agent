"""T6-04：图路径测试的公共工具与夹具。

主图与七条流程子图都用 `astream(stream_mode="updates")` 真跑一遍，断言「节点访问顺序」
而不只看最终输出。多智能体系统的多数 bug 是**编排 bug**——该走的分支没走、循环没有出口、
受理后先问再分流——这类缺陷在最终输出上往往「看起来正常」（模型仍会给出一个合理文本），
只有断言路径才能拦住。

三点必须记住，否则用例会静默写错：

1. **`stream_mode="updates"` 是必需的**。节点名出现在每个更新事件的键里；换成 `"values"`
   拿到的是状态快照而不是节点名，`traced` 会变成空列表，`traced[0]` 直接 `IndexError`。
2. **`traced.extend(event.keys())` 会重复**（同一节点多次执行，如问诊回环），所以位置断言
   一律用 `first_index`（取首次出现）配合 `<` 比较，不用 `==`、`+1`。
3. **子图没有 `intake` / `triage` 节点**。「受理即分流」是主图属性（`intake → triage` 先于
   任何 `subgraph_*`），子图的入口节点是 `observe`（养猪是 `report_guard_entry`）。本阶段文档
   里引用的旧节点名（`intake` / `collect_symptoms` / `diagnose` / `prescribe` / `gate` 作为子图
   首节点）在当前代码中不存在，一律以代码现状为准：主图断言走 `test_main_route.py`，子图断言
   走 `test_subgraphs_path.py`。

`pytest_collection_modifyitems` 给 `tests/graph/` 下的全部用例自动打上 `graph` 标记，这样
`pytest tests/graph -m graph` 能一次跑完本目录所有图测试（T6-04 验收），无需逐文件手写标记。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.budget import ConsultBudget
from app.agent.state import ConsultStage, ProcessId

DEFAULT_THREAD_ID = "graph-path-test"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """给本目录收集到的全部用例打 `graph` 标记（marker 在 pyproject 注册）。"""
    marker = pytest.mark.graph
    for item in items:
        item.add_marker(marker)


def memory_checkpointer() -> InMemorySaver:
    """内存检查点器：图测试一律不连 PostgreSQL（T6-04 验收）。"""
    return InMemorySaver()


async def run_path(app: Any, state: dict, thread_id: str = DEFAULT_THREAD_ID) -> list[str]:
    """跑图并返回访问过的节点顺序（保留重复，供回环断言）。"""
    traced: list[str] = []
    config = {"configurable": {"thread_id": thread_id}}
    async for event in app.astream(state, config, stream_mode="updates"):
        traced.extend(event.keys())
    return traced


def first_index(traced: list[str], node: str) -> int:
    """取节点首次出现的下标；未执行时报出完整实际路径，避免无上下文的 `ValueError`。"""
    assert node in traced, f"节点 {node} 未被执行，实际路径 {traced}"
    return traced.index(node)


def initial_state(**overrides: Any) -> dict:
    """构造一份「红线已装载、流程未定」的基线 state，用例只覆盖关心的键。"""
    base: dict[str, Any] = {
        "session_id": "sess-graph",
        "user_id": "user-graph",
        "process_hint": None,
        "redlines_loaded": True,
        "required_slots": [],
        "config_version": 1,
        "region_code": None,
        "slots": {},
        "prescription_history": [],
        "budget": ConsultBudget().to_state(),
        "messages": [],
        "trace_id": "trace-graph",
    }
    base.update(overrides)
    return base


def subgraph_module(process_id: ProcessId) -> Any:
    """按流程取子图模块，供 `monkeypatch.setattr` 替换叶子工厂。"""
    return importlib.import_module(f"app.agent.graph.subgraphs.{process_id.value}")


# ---- 确定性叶子替身 ----
# 子图文件负责「接线」，智能体行为由各自的 T2 任务覆盖。这里用替身替换会调模型或查库的
# 叶子，保留子图真实的组装代码，让断言只盯「同一份输入走哪条路」。


def _observe_stub() -> Any:
    async def _node(state: Any) -> dict:
        return {"stage": ConsultStage.OBSERVE}

    return _node


def _guard_stub() -> Any:
    """报告守卫替身：未命中，放行（真实信号匹配由 T2-22/T2-28 覆盖）。"""

    async def _node(state: Any) -> dict:
        return {"report_decision": {"triggered": False, "stop_consultation": False}}

    return _node


def _interview_stub(panel_needed: bool) -> Any:
    async def _node(state: Any) -> dict:
        return {"stage": ConsultStage.INTERVIEW, "panel_needed": panel_needed, "slots": {}}

    return _node


def _verify_stub() -> Any:
    async def _node(state: Any) -> dict:
        return {"stage": ConsultStage.VERIFY}

    return _node


def _judge_stub() -> Any:
    async def _node(state: Any) -> dict:
        return {"stage": ConsultStage.JUDGE, "diagnosis": {"confidence_level": "likely", "out_of_scope": False}}

    return _node


def _prescribe_stub() -> Any:
    async def _node(state: Any) -> dict:
        return {"stage": ConsultStage.PRESCRIBE}

    return _node


def _panel_stub() -> Any:
    async def _node(state: Any) -> dict:
        return {"panel_ran": True}

    return _node


def stub_leaf_factories(monkeypatch: pytest.MonkeyPatch, module: Any, *, panel_needed: bool = False) -> None:
    """把子图模块的叶子工厂换成替身，保留子图文件真实的接线代码。

    `make_panel_node` 只在会诊流程（茶 / 辣椒 / 杂草）存在，故按需替换。
    """
    monkeypatch.setattr(module, "make_observe_node", lambda config: _observe_stub())
    monkeypatch.setattr(module, "build_report_guard_node", lambda config: _guard_stub())
    monkeypatch.setattr(module, "make_interview_node", lambda config: _interview_stub(panel_needed))
    monkeypatch.setattr(module, "make_verify_node", lambda config: _verify_stub())
    monkeypatch.setattr(module, "make_judge_node", lambda config: _judge_stub())
    monkeypatch.setattr(module, "make_prescribe_node", lambda config: _prescribe_stub())
    if hasattr(module, "make_panel_node"):
        monkeypatch.setattr(module, "make_panel_node", lambda config: _panel_stub())
