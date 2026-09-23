# T2-24 条件边函数 routes

## 这一项要做什么

把"下一个节点是谁"的全部判断集中到 `app/agent/graph/routes.py`，每个条件边都是只读 `ConsultationState` 的纯函数。方案第六章 6.2 明确：主管式确定性编排的核心是 Supervisor 不是模型，而是状态机本身，因此路由必须可单测、可确定性重放、可断点回溯。本项替换现有 `app/agent/graph.py` 里以 `crop_tag` 与 `round` 为中心的三个内联路由函数（`_route_after_symptoms`、`_route_after_management`、`_route_after_diagnose`），它们把"有没有症状"当成分流依据，属于"先问再分流"，必须整条废弃。

## 前置依赖

- T2-08 ConsultationState 与 ConsultStage 重构：提供 `process_id`、`process_locked`、`stage`、`slots`、`round`、`slot_round`、`budget` 字段与 `ProcessId`、`ConsultStage` 枚举。
- T2-06 流程红线配置（本项只读 `state["redlines_loaded"]`，该键由 intake 写入）；T2-31 预算守卫（路由读 `state["budget"]`，字段名须与 `ConsultBudget` 一致）。
- T2-09 至 T2-12 契约层：`TriageResult.object_confirmed` / `handoff_to_report`、`DiagnosisResult.out_of_scope` / `confidence_level` / `needs_more_info`、`GateResult.decision` / `process_redlines_loaded`、`ReportDecision.stop_consultation` / `triggered` 的字段名。
- 外部环境：无数据库、无 Redis、无模型服务要求，本模块可在纯内存下跑单测。

## 参考章节

- 技术方案 第六章 6.2 主管式确定性编排（`route_after_triage` 原文与四条路由约束）；第七章 7.1 主图结构、7.3 状态机推进规则；第五章 5.3.2 分诊、5.3.4 判定、5.3.6 门禁、5.3.7 报告守卫契约（`observe` 与 `interview` 之后各挂一次检查）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/graph/routes.py` | 全部条件边函数、path_map 常量、`UnroutedStateError` |
| 新增 | `app/agent/graph/__init__.py` | 包声明，导出路由函数与 path_map 常量 |
| 新增 | `tests/graph/test_routes.py` | 每个路由函数的正例、边界例与未覆盖分支异常测试 |
| 新增 | `tests/graph/test_routes_purity.py` | AST 扫描，机器化约束一 |
| 删除 | `app/agent/graph.py` | 三个内联路由函数随旧图移除，`build_graph` 导入改到 `app.agent.graph.main` |

## 实现要点

### 四条路由约束（硬性，不可妥协）

模块顶部只导入状态与枚举，不导入节点、服务或数据库对象：`from app.agent.state import ConsultStage, ConsultationState, ProcessId`。`UnroutedStateError` 继承 `RuntimeError`，消息带函数名与 state 关键字段快照。子图内条件边的 `"handoff"` 键一律映射到子图出口 `"__end__"`，只有主图 `ROUTE_AFTER_SUBGRAPH` 才映射到 `handoff` 节点——子图的边不能指向父图节点，这是 LangGraph 的硬约束，所以同一个返回名在两类 path_map 里指向不同目标。

| 约束 | 落地方式 | 违反后的失败模式 |
| --- | --- | --- |
| 边函数不得有 IO 与模型调用 | 函数体内只出现 `state.get(...)`、比较与布尔运算；不出现 `await`、`db`、`httpx`、`gateway` 字样 | 重放结果不确定，检查点恢复后走到不同分支 |
| 每个分支必须在 path_map 中列出，未覆盖情形抛异常 | 每个条件边配一个 `*_MAP` dict；函数末尾 `raise UnroutedStateError(...)` 而非 `return "default"` | 新增流程后静默走默认分支，路由错误被吞掉 |
| 路由依据必须是枚举或布尔，不得依赖自由文本 | 只读 `ProcessId`、`ConsultStage` 等枚举与 bool；禁止对 `reply`、`summary`、`message` 做关键词匹配 | 自由文本抖动导致同一输入两次路由不一致 |
| 红线配置未加载时直接指向 `handoff` | 首行检查 `state.get("redlines_loaded")`，为假即 fail-closed：主图边返回 `handoff`，子图边返回 `handoff` 并由子图出口交回主图 | 用通用规则代替流程红线继续出方，突破产品说明书 9.3 |

### 入口与分诊路由

`route_after_intake` 是"受理即分流"的唯一入口：intake 完成归属校验与会话装载后立即决定去向，带 `process_hint` 时直达流程子图（如小程序杂草防治按钮），否则进 triage 判流程，不允许先问农户再判流程。`route_after_triage` 对应方案 6.2 原文；主图里 `observe` 位于流程子图内部，因此方案原文的第三个返回名 `observe` 在此改写为 `enter_process`，由 `subgraph_router` 落到具体子图。养猪流程的 pre-observe 检查在 `subgraph_pig` 的入口守卫里（见 T2-28），故 triage 对 `ProcessId.PIG` 仍返回 `enter_process`。

```python
ROUTE_AFTER_INTAKE = {"triage": "triage", "enter_process": "subgraph_router", "handoff": "handoff"}

def route_after_intake(state: ConsultationState) -> str:
    """受理后的第一条路由：受理即分流，先定流程再问问题。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    if state.get("process_locked") and state.get("process_id") is not None:
        return "enter_process"          # 入口直达（process_hint）已锁定流程
    return "triage"

ROUTE_AFTER_TRIAGE = {
    "confirm_object": "confirm_object",   # 对象待确认，插入一次确认交互
    "report_guard": "report_guard",       # 信号命中，先过主图入口守卫
    "enter_process": "subgraph_router",   # 已确认，落到具体流程子图
    "handoff": "handoff",
}

def route_after_triage(state: ConsultationState) -> str:
    """分诊后的路由：纯函数，无 IO，无模型调用。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    triage = state.get("triage") or {}
    if triage.get("handoff_to_report"):
        return "report_guard"          # 报告信号命中，先过守卫再谈其他
    if not triage.get("object_confirmed"):
        return "confirm_object"        # 对象待确认，插入一次确认交互
    return "enter_process"             # 含养猪：其子图入口守卫负责 pre-observe 检查
```

### 确认回环与子图出口

`route_after_report_guard` 在主图上，守卫命中可直接去 `handoff`；`route_after_subgraph_guard` 在子图内，守卫命中只能返回 `handoff`（在子图 path_map 里即出口 `__end__`，见 5.3.7 的二次检查）。`route_after_subgraph` 是子图退出的唯一判据：只有走到 `prescribe`（`stage == PRESCRIBE`）算正常完成，其余一律转人工，这样无论子图因何提前退出都不会误入闸门。

```python
ROUTE_AFTER_CONFIRM_OBJECT = {"triage": "triage", "enter_process": "subgraph_router", "handoff": "handoff"}
ROUTE_AFTER_REPORT_GUARD = {"enter_process": "subgraph_router", "confirm_object": "confirm_object", "handoff": "handoff"}
ROUTE_AFTER_SUBGRAPH_GUARD = {"observe": "observe", "interview": "interview", "handoff": "__end__"}
ROUTE_AFTER_SUBGRAPH = {"gate": "gate", "handoff": "handoff"}

def route_after_confirm_object(state: ConsultationState) -> str:
    """确认对象后的路由：农户未确认或与档案冲突则回分诊重判。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    if state.get("object_confirmed_conflict", False):
        return "triage"
    if state.get("process_id") is None:
        raise UnroutedStateError("route_after_confirm_object", state)
    return "enter_process"

def route_after_report_guard(state: ConsultationState) -> str:
    """主图入口守卫后的路由：命中即转人工，否则进流程子图。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"
    return "confirm_object" if state.get("process_id") is None else "enter_process"

def route_after_subgraph_guard(state: ConsultationState) -> str:
    """子图内守卫后的路由：命中就出子图，否则按 stage 进 observe 或 interview。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"               # handoff 在子图 path_map 里即出口
    if state.get("stage") in (ConsultStage.INTAKE, ConsultStage.TRIAGE):
        return "observe"               # 养猪：入口守卫放行后才看现场
    return "interview"                 # 其余流程：observe 之后的兜底检查

def route_after_subgraph(state: ConsultationState) -> str:
    """子图出口路由：只有走到 prescribe 才算正常完成，其余一律转人工。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    diagnosis = state.get("diagnosis") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"
    if diagnosis.get("out_of_scope"):
        return "handoff"
    return "gate" if state.get("stage") == ConsultStage.PRESCRIBE else "handoff"
```

### 问诊与判定路由

`verify → judge` 与主图 `followup → END` 都用普通边，因此没有 `route_after_verify` 与 `route_after_followup`。本组三个 path_map 都挂在子图内部，故 `handoff` 键均映射为 `"__end__"`。

```python
ROUTE_AFTER_JUDGE = {"interview": "interview", "prescribe": "prescribe", "handoff": "__end__"}
PANEL_ENABLED_PROCESSES = frozenset({ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED})
ROUTE_AFTER_INTERVIEW = {"interview": "interview", "verify": "verify", "panel": "panel", "handoff": "__end__"}

def route_after_interview(state: ConsultationState) -> str:
    """问诊后路由：命中报告信号先转人工；疑似用药先会诊；槽位齐备推进。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"              # 5.3.7：问诊之后的第二次守卫检查
    budget = state.get("budget") or {}
    if slots_ready(state):
        if state.get("process_id") in PANEL_ENABLED_PROCESSES and state.get("panel_needed"):
            return "panel"
        return "verify"
    if budget.get("interview_rounds", 0) >= budget.get("max_interview_rounds", 4):
        return "handoff"              # 连续未收敛，输出携带已确认信息
    return "interview"

def route_after_judge(state: ConsultationState) -> str:
    """判定后的路由：超边界或回环用尽则转人工，否则可开方。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    diagnosis = state.get("diagnosis") or {}
    budget = state.get("budget") or {}
    if diagnosis.get("out_of_scope"):
        return "handoff"              # 超能力边界，直达转人工
    level = diagnosis.get("confidence_level")
    if level == "uncertain" and (diagnosis.get("needs_more_info") or []):
        if budget.get("judge_loops", 0) < budget.get("max_judge_loops", 2):
            return "interview"        # 置信不足且还有问可问，反向补问
        return "handoff"
    if level in ("likely", "preliminary"):
        return "prescribe"
    raise UnroutedStateError("route_after_judge", state)

ROUTE_AFTER_GATE = {"followup": "followup", "handoff": "handoff"}

def route_after_gate(state: ConsultationState) -> str:
    """门禁后的路由：allow 去登记回访，degrade/block 一律转人工。"""
    gate = state.get("gate_result") or {}
    if not gate.get("process_redlines_loaded", False):
        return "handoff"              # fail-closed
    decision = gate.get("decision")
    if decision == "allow":
        return "followup"
    if decision in ("degrade", "block"):
        return "handoff"
    raise UnroutedStateError("route_after_gate", state)
```

`route_after_gate` 不查 `redlines_loaded` 而查 `gate_result.process_redlines_loaded`：门禁结论是权威来源，state 上的 `redlines_loaded` 只是入口快照，两者不一致时以门禁为准。`slots_ready(state)` 只做集合运算：把流程配置 `required_slots` 中 `required=True` 的键取出，减去 `slots` 里 `status in {"filled", "unknown"}` 的键，空集即齐备；`unknown` 是合法答案，不得视为缺口。

## 验收标准

- `ruff check`、`ruff format --check`、`mypy app/agent/graph/routes.py` 三条命令均返回 0。
- `pytest tests/graph/test_routes.py` 全部通过，覆盖：`route_after_intake` 在 `process_locked=True` 时返回 `enter_process`；`route_after_triage` 在 `handoff_to_report=True` 时返回 `report_guard`；`route_after_judge` 在 `out_of_scope=True` 时返回 `handoff`；`route_after_subgraph` 在 `stage=PRESCRIBE` 时返回 `gate`，其余 stage 一律返回 `handoff`。
- 四条路由约束各有对应测试：`redlines_loaded=False` 时全部路由 fail-closed（主图边返回 `handoff`，子图边返回 `handoff` 并落到 `<子图>__end__`）；不满足任何分支时抛出 `UnroutedStateError` 且消息含函数名。
- `tests/graph/test_routes_purity.py` 用 `ast` 扫描 `routes.py`，断言函数体内不出现 `await`、`db`、`gateway`、`httpx` 标识符；并参数化断言每个路由函数的可能返回值集合与其同名 path_map 的键集合完全相等。
- `grep -c '"handoff": "__end__"' app/agent/graph/routes.py` 输出 4（四个子图 path_map），而 `ROUTE_AFTER_SUBGRAPH` 与主图 path_map 的 `handoff` 键映射到节点名。
