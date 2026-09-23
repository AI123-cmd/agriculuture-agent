# T2-24 条件边函数 routes

## 这一项要做什么

把"下一个节点是谁"的全部判断集中到 `app/agent/graph/routes.py`，每个条件边都是一个只读 `ConsultationState` 的纯函数。方案第六章 6.2 明确：主管式确定性编排的核心是 Supervisor 不是模型，而是状态机本身，因此路由必须可单测、可确定性重放、可断点回溯。本项替换现有 `app/agent/graph.py` 里以 `crop_tag` 与 `round` 为中心的三个内联路由函数（`_route_after_symptoms`、`_route_after_management`、`_route_after_diagnose`）。这三个函数把"有没有症状"当成分流依据，属于"先问再分流"，必须整条废弃。

## 前置依赖

- T2-08 ConsultationState 与 ConsultStage 重构：提供 `process_id`、`process_locked`、`stage`、`slots`、`round`、`slot_round`、`budget` 字段与 `ProcessId`、`ConsultStage` 枚举。
- T2-06 流程红线配置模型与完整性校验：本项只读 `state["redlines_loaded"]`，该键由 intake 节点写入。T2-31 轮次与成本预算守卫：路由读取 `state["budget"]` 上的计数与上限，字段名必须与 `ConsultBudget` 一致。
- T2-09 至 T2-12 契约层：`TriageResult.object_confirmed` / `handoff_to_report`、`DiagnosisResult.out_of_scope` / `confidence_level` / `needs_more_info`、`GateResult.decision` / `process_redlines_loaded`、`ReportDecision.stop_consultation` 的字段名。
- 外部环境：无数据库、无 Redis、无模型服务要求，本模块可在纯内存下跑单测。

## 参考章节

- 技术方案 第六章 6.2 主管式确定性编排（`route_after_triage` 原文与四条路由约束）
- 技术方案 第七章 7.1 主图结构（分支名称与去向）、7.3 状态机推进规则（轮次与预算）
- 技术方案 第五章 5.3.2 分诊契约、5.3.4 判定契约、5.3.6 门禁契约、5.3.7 报告守卫契约

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

模块顶部只导入状态与枚举，不导入任何节点、服务或数据库对象：`from app.agent.state import ConsultStage, ConsultationState, ProcessId`。`UnroutedStateError` 继承 `RuntimeError`，消息带函数名与 state 关键字段快照，便于日志定位。

| 约束 | 落地方式 | 违反后的失败模式 |
| --- | --- | --- |
| 边函数不得有 IO 与模型调用 | 函数体内只出现 `state.get(...)`、比较与布尔运算；不出现 `await`、`db`、`httpx`、`gateway` 字样 | 重放结果不确定，检查点恢复后走到不同分支 |
| 每个分支必须在 path_map 中列出，未覆盖情形抛异常 | 每个条件边配一个 `*_MAP` dict；函数末尾 `raise UnroutedStateError(...)` 而非 `return "default"` | 新增流程后静默走默认分支，路由错误被吞掉 |
| 路由依据必须是枚举或布尔，不得依赖自由文本 | 只读 `ProcessId`、`ConsultStage` 等枚举与 bool；禁止对 `reply`、`summary`、`message` 做关键词匹配 | 自由文本抖动导致同一输入两次路由不一致 |
| 红线配置未加载时直接指向 `handoff` | 每个路由函数首行检查 `state.get("redlines_loaded")`，为假即 `return "handoff"` | 用通用规则代替流程红线继续出方，突破产品说明书 9.3 |

### 入口与分诊路由

`route_after_intake` 是"受理即分流"的唯一入口：intake 完成归属校验与会话装载后立即决定去向，带 `process_hint` 时直达流程子图（如小程序杂草防治按钮），否则进 triage 判流程，不允许先问农户再判流程。`route_after_triage` 对应方案 6.2 原文，方案返回名是 `report_guard` / `confirm_object` / `observe`；主图里 `observe` 位于流程子图内部，因此第三个分支返回 `enter_process`，由 `subgraph_router` 落到具体子图。

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
    "report_guard": "report_guard",       # 养猪流程先过守卫再看现场
    "enter_process": "subgraph_router",   # 已确认，落到具体流程子图
    "handoff": "handoff",
}


def route_after_triage(state: ConsultationState) -> str:
    """分诊后的路由：纯函数，无 IO，无模型调用。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    triage = state.get("triage") or {}
    if triage.get("handoff_to_report"):
        return "report_guard"          # 养猪流程直接转报告守卫
    if not triage.get("object_confirmed"):
        return "confirm_object"        # 对象待确认，插入一次确认交互
    if triage.get("process_id") == ProcessId.PIG:
        return "report_guard"          # 养猪流程先过守卫再看现场
    return "enter_process"
```

### 确认回环、报告守卫旁路与现场核查

```python
ROUTE_AFTER_CONFIRM_OBJECT = {"triage": "triage", "enter_process": "subgraph_router", "handoff": "handoff"}
ROUTE_AFTER_REPORT_GUARD = {"enter_process": "subgraph_router", "observe": "observe", "handoff": "handoff"}
ROUTE_AFTER_OBSERVE = {"interview": "interview", "handoff": "handoff"}


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
    """报告守卫旁路：命中即转人工，已收集内容保留但不继续追问。"""
    if not state.get("redlines_loaded", False):
        return "handoff"
    decision = state.get("report_decision") or {}
    if decision.get("stop_consultation") or decision.get("triggered"):
        return "handoff"
    # 主图入口处的守卫通过后进子图；子图内的守卫通过后继续 observe
    return "enter_process" if state.get("stage") == ConsultStage.TRIAGE else "observe"


def route_after_observe(state: ConsultationState) -> str:
    if not state.get("redlines_loaded", False):
        return "handoff"
    return "interview"
```

### 问诊、核查与判定路由

```python
ROUTE_AFTER_VERIFY = {"judge": "judge", "handoff": "handoff"}
ROUTE_AFTER_FOLLOWUP = {"end": "__end__"}
ROUTE_AFTER_JUDGE = {"interview": "interview", "prescribe": "prescribe", "handoff": "handoff"}
PANEL_ENABLED_PROCESSES = frozenset({ProcessId.TEA, ProcessId.CHILI, ProcessId.WEED})

# 未收敛且未超轮次则继续问；关键槽位齐备则推进；疑似用药先并行会诊；轮次用尽转人工
ROUTE_AFTER_INTERVIEW = {
    "interview": "interview",
    "verify": "verify",
    "panel": "panel",
    "handoff": "handoff",
}


def route_after_interview(state: ConsultationState) -> str:
    """问诊后的路由：槽位齐备则推进；未齐备且轮次用尽则转人工。"""
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
    if not state.get("redlines_loaded", False):
        return "handoff"
    return "judge"


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


ROUTE_AFTER_GATE = {"followup": "followup", "handoff": "handoff", "blocked": "handoff"}


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
- 四条路由约束各有对应测试：构造 `redlines_loaded=False` 的 state，断言全部路由函数返回 `handoff`；构造不满足任何分支的 state，断言抛出 `UnroutedStateError` 且消息含函数名。
- `tests/graph/test_routes_purity.py` 用 `ast` 扫描 `routes.py`，断言函数体内不出现 `await`、`db`、`gateway`、`httpx` 标识符；并参数化断言每个路由函数的可能返回值集合与其同名 path_map 的键集合完全相等，缺一个键即失败。
