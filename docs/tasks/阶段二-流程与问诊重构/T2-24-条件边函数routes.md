# T2-24 条件边函数 routes

## 这一项要做什么

把"下一个节点是谁"的全部判断集中到 `app/agent/graph/routes.py`，每个条件边都是只读 `ConsultationState` 的纯函数。方案第六章 6.2 明确：主管式确定性编排的核心是 Supervisor 不是模型，而是状态机本身，因此路由必须可单测、可确定性重放、可断点回溯。本项替换现有 `app/agent/graph.py` 里以 `crop_tag` 与 `round` 为中心的三个内联路由函数（`_route_after_symptoms`、`_route_after_management`、`_route_after_diagnose`）——它们把"有没有症状"当成分流依据，属于"先问再分流"，必须整条废弃。

## 前置依赖

- T2-08 ConsultationState 与 ConsultStage 重构：提供 `process_id`、`process_locked`、`stage`、`slots`、`round`、`slot_round`、`budget` 字段与 `ProcessId`、`ConsultStage` 枚举。
- T2-06 流程红线配置：本项只读 `state["redlines_loaded"]`，该键由 intake 写入。T2-31 预算守卫：路由读取 `state["budget"]` 的计数与上限，字段名必须与 `ConsultBudget` 一致。
- T2-09 至 T2-12 契约层：`TriageResult.object_confirmed` / `handoff_to_report`、`DiagnosisResult.out_of_scope` / `confidence_level` / `needs_more_info`、`GateResult.decision` / `process_redlines_loaded`、`ReportDecision.stop_consultation` 的字段名。
- 外部环境：无数据库、无 Redis、无模型服务要求，本模块可在纯内存下跑单测。

## 参考章节

- 技术方案 第六章 6.2 主管式确定性编排（`route_after_triage` 原文与四条路由约束）
- 技术方案 第七章 7.1 主图结构、7.3 状态机推进规则；第五章 5.3.2 分诊、5.3.4 判定、5.3.6 门禁、5.3.7 报告守卫契约

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/graph/__init__.py` | 包声明，导出路由函数与 path_map 常量 |
| 新增 | `app/agent/graph/routes.py` | 全部条件边函数、path_map 常量、`UnroutedStateError` |
| 新增 | `tests/graph/test_routes.py` | 每个路由函数的正例、边界例与未覆盖分支异常测试 |
| 新增 | `tests/graph/test_routes_purity.py` | AST 扫描，机器化约束一 |
| 删除 | `app/agent/graph.py` | 三个内联路由函数随旧图一并移除 |
| 修改 | `app/agent/__init__.py` | `build_graph` 的导入来源改到 `app.agent.graph.main` |

## 实现要点

### 模块定位与四条路由约束（硬性，不可妥协）

模块顶部只导入状态与枚举，不导入节点、服务或数据库对象：`from app.agent.state import ConsultStage, ConsultationState, ProcessId`。`UnroutedStateError` 继承 `RuntimeError`，消息带函数名与 state 关键字段快照，便于日志定位。

| 约束 | 落地方式 | 违反后的失败模式 |
| --- | --- | --- |
| 边函数不得有 IO 与模型调用 | 函数体内只出现 `state.get(...)`、比较与布尔运算；不出现 `await`、`db`、`httpx`、`gateway` 字样 | 重放结果不确定，检查点恢复后走到不同分支 |
| 每个分支必须在 path_map 中列出，未覆盖情形抛异常 | 每个条件边配一个 `*_MAP` dict；函数末尾 `raise UnroutedStateError(...)` 而非 `return "default"` | 新增流程后静默走默认分支，路由错误被吞掉 |
| 路由依据必须是枚举或布尔，不得依赖自由文本 | 只读 `ProcessId`、`ConsultStage` 等枚举与 bool；禁止对 `reply`、`summary`、`message` 做关键词匹配 | 自由文本抖动导致同一输入两次路由不一致 |
| 红线配置未加载时直接指向 `handoff` | 首行检查 `state.get("redlines_loaded")`，为假即 fail-closed：主图边返回 `handoff`，子图边返回出口 `end` 并由主图导向 `handoff` | 用通用规则代替流程红线继续出方，突破产品说明书 9.3 |

### 入口与分诊路由

`route_after_intake` 是"受理即分流"的唯一入口：intake 完成归属校验与会话装载后立即决定去向，带 `process_hint` 时直达流程子图（如小程序杂草防治按钮），否则进 triage 判流程，不允许先问农户再判流程。`route_after_triage` 对应方案 6.2 原文；主图里 `observe` 位于流程子图内部，因此方案原文的第三个返回名 `observe` 在此改写为 `enter_process`，由 `subgraph_router` 落到具体子图。养猪流程的 pre-observe 检查在 `subgraph_pig` 的入口守卫里（见 T2-28），故 triage 对 `ProcessId.PIG` 仍返回 `enter_process`，只有 `handoff_to_report` 直接命中主图 `report_guard`。

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

### 确认回环与报告守卫

`route_after_report_guard` 与 `route_after_subgraph_guard` 的分工必须分清：前者在主图上，命中可直接去 `handoff`；后者在子图内，命中只能返回子图出口 `end`，因为子图的边不能指向父图节点，最终由主图的 `route_after_subgraph` 统一导向 `handoff`。这条是 LangGraph 的硬约束，不是风格选择。

```python
ROUTE_AFTER_CONFIRM_OBJECT = {"triage": "triage", "enter_process": "subgraph_router", "handoff": "handoff"}
ROUTE_AFTER_REPORT_GUARD = {"enter_process": "subgraph_router", "confirm_object": "confirm_object", "handoff": "handoff"}
ROUTE_AFTER_OBSERVE = {"report_guard": "report_guard", "end": "__end__"}
ROUTE_AFTER_SUBGRAPH_GUARD = {"interview": "interview", "end": "__end__"}
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


def route_after_observe(state: ConsultationState) -> str:
    """现场核查后一律先过子图内的报告守卫兜底检查，红线缺失则直接出子图。"""
    if not state.get("redlines_loaded", False):
        return "end"
    return "report_guard"


def route_after_subgraph_guard(state: ConsultationState) -> str:
    """子图内守卫后的路由：命中或红线缺失都出子图，交由主图转人工。"""
    if not state.get("redlines_loaded", False):
        return "end"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "end"
    return "interview"


def route_after_subgraph(state: ConsultationState) -> str:
    """子图出口检查：守卫命中或红线缺失走 handoff，否则进闸门。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"
    return "gate"
```

### 问诊、核查与判定路由

```python
ROUTE_AFTER_VERIFY = {"judge": "judge", "handoff": "handoff"}
ROUTE_AFTER_FOLLOWUP = {"end": "__end__"}
ROUTE_AFTER_JUDGE = {"interview": "interview", "prescribe": "prescribe", "handoff": "handoff"}
PANEL_ENABLED_PROCESSES = frozenset({ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED})
ROUTE_AFTER_INTERVIEW = {"interview": "interview", "verify": "verify", "panel": "panel", "handoff": "handoff"}


def route_after_interview(state: ConsultationState) -> str:
    """问诊后路由：疑似用药先会诊；槽位齐备则推进；未齐备且轮次用尽则转人工。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    budget = state.get("budget") or {}
    if slots_ready(state):
        if state.get("process_id") in PANEL_ENABLED_PROCESSES and state.get("panel_needed"):
            return "panel"
        return "verify"
    if budget.get("interview_rounds", 0) >= budget.get("max_interview_rounds", 4):
        return "handoff"              # 连续未收敛，输出携带已确认信息
    return "interview"


def route_after_verify(state: ConsultationState) -> str:
    """核查后的路由：唯一出口是 judge，红线未加载则 fail-closed 去 handoff。"""
    return "handoff" if not state.get("redlines_loaded", False) else "judge"


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


def route_after_followup(state: ConsultationState) -> str:
    return "end"
```

`route_after_gate` 不检查 `redlines_loaded` 而检查 `gate_result.process_redlines_loaded`：门禁结论是权威来源，state 上的 `redlines_loaded` 只是入口快照，两者不一致时以门禁为准。`slots_ready(state)` 只做集合运算：把流程配置 `required_slots` 中 `required=True` 的键取出，减去 `slots` 里 `status in {"filled", "unknown"}` 的键，空集即齐备；`unknown` 是合法答案，不得视为缺口。

## 验收标准

- `ruff check app/agent/graph/routes.py`、`ruff format --check`、`mypy app/agent/graph/routes.py` 三条命令均返回 0。
- `pytest tests/graph/test_routes.py` 全部通过，覆盖：`route_after_intake` 在 `process_locked=True` 时返回 `enter_process`；`route_after_triage` 在 `handoff_to_report=True` 时返回 `report_guard`；`route_after_judge` 在 `out_of_scope=True` 时返回 `handoff`。
- 四条路由约束各有对应测试：构造 `redlines_loaded=False` 的 state，断言全部路由函数 fail-closed（主图边返回 `handoff`，子图边返回 `end`）；构造不满足任何分支的 state，断言抛出 `UnroutedStateError` 且消息含函数名。
- `tests/graph/test_routes_purity.py` 用 `ast` 扫描 `routes.py`，断言函数体内不出现 `await`、`db`、`gateway`、`httpx` 标识符；并参数化断言每个路由函数的可能返回值集合与其同名 path_map 的键集合完全相等，缺一个键即失败。
