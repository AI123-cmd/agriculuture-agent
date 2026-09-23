# T2-25 主图组装 main

## 这一项要做什么

把 `app/agent/graph/main.py` 写成一张完整的主图，节点序列严格为 intake → triage →（confirm_object 回环）→ 流程子图 → observe → report_guard 兜底检查 → interview →（未收敛回环）→ verify →（panel 旁路）→ judge →（回环/转人工/开方）→ prescribe → gate →（allow 去 followup，degrade/block 去 handoff）→ followup → END。现有的 `app/agent/graph.py` 是一条 `intake → collect_symptoms → collect_management → fetch_environment → diagnose → prescribe` 的线性链，既没有分诊、也没有闸门、也没有回环出口，本项将其整体替换。

本项最不可妥协的一点是**受理即分流**：路由必须前移到 intake 之后，第一条边就走出"进哪条流程"。产品说明书 9.2 第 11 条要求"问诊第一环节即确定流程归属，而非先收集症状再判断"。`build_graph` 编译后必须满足 `trace[0] == "intake"` 且 `trace[1] in {"triage", "subgraph_router"}`，任何"先问症状"的路径都算不合格。

## 前置依赖

- T2-24 条件边函数 routes：提供 `route_after_intake` / `route_after_triage` / `route_after_report_guard` / `route_after_confirm_object` / `route_after_subgraph` 与全部 path_map 常量、`UnroutedStateError`。
- T2-26 流程子图工厂函数：提供 `make_observe_node` / `make_interview_node` / `make_verify_node` / `make_judge_node` / `make_prescribe_node`。
- T2-22 报告守卫 ReportGuard：提供 `build_report_guard_node(config=None)`，`config=None` 时节点按 state 上的 `process_id` 取流程红线。
- T2-27 至 T2-29 七条流程子图：提供 `SUBGRAPH_BUILDERS` 注册表，子图内部自带 `observe` 至 `prescribe`。
- T2-23 转人工上下文包生成器：提供 `build_handoff_node()`。
- T2-30 Postgres 检查点器：`build_graph()` 编译时必须能拿到 checkpointer 单例。
- T2-31 轮次与成本预算守卫：`intake` 节点需在 state 上初始化 `budget`。
- T2-45 闸门子图接入：`gate` 作为一个子图挂载，具备自己的入口与出口。
- 外部环境：PostgreSQL 16+ 可连通（编译期需 checkpointer）；`USE_MOCK_LLM=true` 时全部节点可离线跑通。

## 参考章节

- 技术方案 第七章 7.1 主图结构（mermaid 全图，节点名与分支名以图为准）；7.2 代码结构（`graph/main.py`、`graph/routes.py`、`graph/subgraphs/`、`graph/gate_graph.py`）；7.3 状态机推进规则（回环出口、中断与恢复）；7.5 检查点实现（编译时注入 checkpointer）
- 技术方案 第五章 5.3.7 报告守卫契约（observe 之后兜底检查的挂载点）；第八章 8.1 闸门在链路中的位置（gate 在 prescribe 与 followup 之间）
- 产品说明书 9.2 第 11 条 受理即分流

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/graph/main.py` | 主图组装：节点注册、边、条件边、子图挂载、编译 |
| 新增 | `app/agent/graph/__init__.py` | 若 T2-24 未建，则在 T2-24 建，本项只补充导出 |
| 修改 | `app/agent/__init__.py` | `from .graph.main import build_graph`，替换旧的 `.graph` 导入 |
| 删除 | `app/agent/graph.py` | 旧线性图与三个内联路由函数整体移除 |
| 新增 | `tests/graph/test_main_graph.py` | 主图路径测试：受理即分流、回环出口、闸门分支 |

## 实现要点

### 节点清单与归属

主图节点为 `intake`（受理，写 `budget`、`process_locked` 与 `redlines_loaded`）、`triage`（分诊）、`confirm_object`（对象确认，模板文案）、`subgraph_router`（纯路由）、`report_guard`（分诊命中报告信号时的入口守卫）、`followup`（登记回访）、`handoff`（转人工）。子图节点为 `observe`、`report_guard`（兜底检查）、`interview`、`panel`（仅茶/辣椒/杂草）、`verify`、`judge`、`prescribe`，闸门子图为 `gate`。`subgraph_router`、`followup`、`handoff`、`report_guard`、`gate` 与 `confirm_object` 均不调模型。

### 子图内节点与主图的边界

方案 7.4 要求子图结构与主链路一致、差异全部来自配置，因此子图内部自带 `observe → report_guard → interview → verify → judge → prescribe`，其中 `report_guard` 就是方案 7.1 图里的 RG2 兜底检查。主图不重复注册这六个节点，而是通过 `subgraph_router` 把控制交给选中子图，子图执行到 `prescribe` 末尾后用 `END` 交回主图。

这里有一条 LangGraph 硬约束必须遵守：子图内部的边只能指向本子图的节点，不能指向父图节点，所以 `report_guard` 必须实例化在子图内部（由 `make_report_guard_node(config)` 产出），守卫命中时只能用子图出口 `__end__` 结束，再由主图的 `route_after_subgraph` 统一导向 `handoff`。方案 7.1 mermaid 把子图内部节点画在主链上是逻辑展开视图，落到实现即"子图自带守卫 + 主图看子图为单一节点"。同时 mermaid 里 `ROUTE -->|pig| RG1` 的入口守卫也在养猪子图内部实现（见 T2-28），主图上的 `report_guard` 节点只服务分诊阶段就命中报告信号的早退场景。

### 主图组装完整代码

```python
"""主图组装：受理即分流 + 七条流程子图 + 闸门 + 回环出口。"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agent.agents.handoff import build_handoff_node
from app.agent.agents.report_guard import build_report_guard_node
from app.agent.agents.triage import run_triage
from app.agent.budget import ConsultBudget
from app.agent.checkpointer import get_checkpointer
from app.agent.graph.routes import (
    ROUTE_AFTER_CONFIRM_OBJECT, ROUTE_AFTER_GATE, ROUTE_AFTER_INTAKE,
    ROUTE_AFTER_REPORT_GUARD, ROUTE_AFTER_SUBGRAPH, ROUTE_AFTER_TRIAGE,
    UnroutedStateError, route_after_confirm_object, route_after_gate,
    route_after_intake, route_after_report_guard, route_after_subgraph,
    route_after_triage,
)
from app.agent.graph.subgraphs import SUBGRAPH_BUILDERS
from app.agent.state import ConsultStage, ConsultationState, ProcessId


async def intake(state: ConsultationState, config=None) -> dict:
    """受理：归属校验 + 会话装载 + 预算初始化 + 红线配置加载。

    受理即分流的落点：本节点之后的第一条边就是流程路由，不再有"先问症状"。
    """
    process_hint = state.get("process_hint")
    locked = process_hint is not None      # 小程序按钮直达，跳过 triage 提问
    return {
        "session_id": state.get("session_id"),
        "stage": ConsultStage.INTAKE,
        "budget": ConsultBudget.initial().to_state(),
        "process_locked": locked,
        "process_id": ProcessId(process_hint) if locked else None,
        "redlines_loaded": state.get("redlines_loaded", False),  # 由 config 注入
    }


def confirm_object(state: ConsultationState, config=None) -> dict:
    """一次性对象确认：农户答复后回写 process_id 并锁定。"""
    answer_process = state.get("confirm_object_answer")
    if answer_process is None:
        return {
            "reply": f"你问的是{state.get('object_label') or '哪一对象'}吗？确认一下我好对症下药。",
            "stage": ConsultStage.TRIAGE,
            "awaiting_object_confirm": True,
        }
    return {
        "process_id": ProcessId(answer_process),
        "process_locked": True,
        "object_confirmed_conflict": answer_process != state.get("profile_process_hint"),
        "stage": ConsultStage.TRIAGE,
    }


def subgraph_router(state: ConsultationState, config=None) -> dict:
    """纯路由节点：把控制交给选中子图，本身不改 state。"""
    return {}


def followup(state: ConsultationState, config=None) -> dict:
    """登记回访：处方卡通过门禁后调用回访服务，并投递 prescription.created。"""
    return {"stage": ConsultStage.FOLLOWUP}


def route_to_subgraph(state: ConsultationState) -> str:
    """按 process_id 选子图，未注册即抛错（不允许静默兜底）。"""
    process_id = state.get("process_id")
    if process_id is None or process_id not in SUBGRAPH_BUILDERS:
        raise UnroutedStateError("route_to_subgraph", state)
    return f"subgraph_{process_id.value}"


async def build_graph():
    """构建并编译主图（含 Postgres checkpointer）。"""
    graph = StateGraph(ConsultationState)

    # 主图节点
    graph.add_node("intake", intake)
    graph.add_node("triage", run_triage)
    graph.add_node("confirm_object", confirm_object)
    graph.add_node("subgraph_router", subgraph_router)
    graph.add_node("report_guard", build_report_guard_node())   # config=None：按 state 取红线
    graph.add_node("followup", followup)
    graph.add_node("handoff", build_handoff_node())

    # 七条流程子图（子图内部自带 observe / report_guard / interview / verify / judge / prescribe）
    for process_id, builder in SUBGRAPH_BUILDERS.items():
        graph.add_node(f"subgraph_{process_id.value}", builder())

    from app.agent.graph.gate_graph import build_gate_subgraph   # 闸门子图
    graph.add_node("gate", build_gate_subgraph())

    # 入口：START -> intake（受理即分流从这里开始）
    graph.add_edge(START, "intake")

    # 第一条条件边：受理后立刻决定"判流程"还是"直达流程子图"
    graph.add_conditional_edges("intake", route_after_intake, ROUTE_AFTER_INTAKE)

    # 分诊：判流程 / 对象确认 / 报告信号命中先过守卫 / 进流程子图
    graph.add_conditional_edges("triage", route_after_triage, ROUTE_AFTER_TRIAGE)
    graph.add_conditional_edges("confirm_object", route_after_confirm_object, ROUTE_AFTER_CONFIRM_OBJECT)
    graph.add_conditional_edges("report_guard", route_after_report_guard, ROUTE_AFTER_REPORT_GUARD)

    # 子图分发：按 process_id 进对应子图；子图出口按守卫结论落到闸门或转人工
    graph.add_conditional_edges(
        "subgraph_router", route_to_subgraph,
        {f"subgraph_{p.value}": f"subgraph_{p.value}" for p in ProcessId},
    )
    for process_id in SUBGRAPH_BUILDERS:
        graph.add_conditional_edges(
            f"subgraph_{process_id.value}", route_after_subgraph, ROUTE_AFTER_SUBGRAPH,
        )

    # 闸门出口：allow -> followup -> END；degrade/block -> handoff -> END
    graph.add_conditional_edges("gate", route_after_gate, ROUTE_AFTER_GATE)
    graph.add_edge("followup", END)
    graph.add_edge("handoff", END)

    return graph.compile(checkpointer=await get_checkpointer())
```

### 子图内部必须实现的四条边

主图无法反向注册子图内部节点，因此子图文件（T2-27 至 T2-29）必须自己实现以下四条边，且边函数一律从 `routes.py` 取：`graph.add_edge("observe", "report_guard")`（普通边，看现场后交给兜底检查）；`graph.add_conditional_edges("report_guard", route_after_subgraph_guard, ROUTE_AFTER_SUBGRAPH_GUARD)`（键含 `observe` / `interview` / `handoff`，其中 `handoff` 映射子图出口 `__end__`，命中或不放行时出子图）；`graph.add_conditional_edges("interview", route_after_interview, ROUTE_AFTER_INTERVIEW)`（含 `interview` 回环、`verify`、`panel`、`handoff`）；`graph.add_conditional_edges("judge", route_after_judge, ROUTE_AFTER_JUDGE)` 与 `graph.add_edge("prescribe", END)`（`END` 指子图出口，交回主图）。养猪子图是唯一例外：入口多一个守卫节点 `report_guard_entry`（见 T2-28）。

### 为什么"受理即分流"必须前移

现有链路的第一个功能性节点是 `collect_symptoms`，分流靠 `crop_tag` 这个来自请求的自由文本字段，等价于"先问一句再猜是哪条流程"。这带来三个问题：农户被问了与流程无关的问题；同一条症状描述在多条流程下给出不同追问；`process_id` 迟迟未定，红线配置无法加载。前移到 intake 之后，`process_id` 在第二个节点就被锁定，红线随之加载，后续每个节点都以确定的流程配置为输入。

### 图编译与单例

`build_graph()` 是 async，因为要 `await get_checkpointer()`。单例仍放在 `app/agent/__init__.py` 的 `get_agent()`，但去掉现有的 `_agent_loop` 事件循环判断——Postgres checkpointer 用连接池，不绑事件循环，跨循环复用同一个 saver 是安全的（理由见 T2-30）。

## 验收标准

- `pytest tests/graph/test_main_graph.py` 全部通过，其中包含：
  - 用 `astream(stream_mode="updates")` 跑一条水稻流程，断言 `trace[0] == "intake"` 且 `trace[1] in {"triage", "subgraph_rice"}`，即受理即分流成立。
  - 构造 `handoff_to_report=True` 的分诊结果，断言路径为 `intake → triage → report_guard → handoff`，且未进入任何 `subgraph_*`。
  - 构造养猪流程（`ProcessId.PIG`），断言子图内 `report_guard` 出现在 `observe` 之前。
  - 构造 `interview_rounds=4` 且槽位仍 missing 的 state，断言路径出口是 `handoff` 而非无限 `interview`。
  - 构造 `gate_result.decision="degrade"`，断言路径为 `gate → handoff`，`prescription` 不出现在最终 `reply` 中。
- `python -c "from app.agent.graph.main import build_graph"` 无导入错误，且 `build_graph()` 能返回编译后的图对象（PostgreSQL 已启动时）。
- `ruff check`、`ruff format --check`、`mypy app` 均返回 0。
- `grep -n "collect_symptoms\|collect_management\|fetch_environment" app/agent/graph/main.py` 无输出，确认旧链路节点未被复用。
