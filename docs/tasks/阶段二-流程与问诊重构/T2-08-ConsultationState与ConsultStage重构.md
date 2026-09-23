# T2-08 ConsultationState 与 ConsultStage 重构

## 这一项要做什么

把 `app/agent/state.py` 从当前的 `AgentState`（以 `crop_tag`、`symptoms`、`step` 为核心的会话式状态）重构为技术方案 5.3.1 定义的 `ConsultationState` 黑板式共享状态。新状态加 `slots`、`observations`、`evidence`、`diagnosis`、`prescription`、`gate_result`、`report_decision` 等键，并以 `stage: ConsultStage` 取代原来的 `step: str`。

重构的理由是当前状态把事实散落在 `symptoms`、`management`、`environment` 三个弱类型字典里，没有来源、没有状态、没有归属约束，任何智能体都能覆盖任何字段。黑板式状态把每个事实收进带 `status` 与 `source` 的 `SlotValue`，让"单一写入者""只写不覆盖""带状态写入"三条规则可以被代码执行。

## 前置依赖

- 依赖 T2-01：必须已提供 `ProcessId`、`ConsultStage`、`SlotSource`、`SlotStatus`、`SOURCE_PRIORITY`
- 依赖 T2-02：必须已提供 `ProcessConfig` 与 `SlotSpec`（写入校验要查 `owner`）
- 外部条件：删除现有 `app/agent/graph.py` 与 `app/agent/nodes/` 的引用要同步进行，但**不要在更早的任务里删 `ai_router/`**
- 与 T2-24 的关系：本项只定义状态与写入函数，条件边函数在 T2-24

## 参考章节

- 技术方案 第5章 5.3.1 共享状态 ConsultationState（`SlotValue`、`EvidenceItem`、`ConsultationState` 全字段）
- 技术方案 第5章 5.3.1 `ConsultStage` 的十一个取值
- 技术方案 第6章 6.3 黑板式共享状态（三条读写规则与来源优先级表）
- 技术方案 第6章 6.3 多地块的处理（`farm_plot_id` 必填，槽位键加地块前缀）
- 技术方案 第7章 7.3 状态机推进规则（轮次与预算字段）
- 技术方案 第12章 12.4 问诊与处方卡（`process_locked` 与 `config_version`）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 修改 | `app/agent/state.py` | 保留 T2-01 的枚举，新增 `SlotValue`、`EvidenceItem`、`ConsultationState` 与写入函数 |
| 新增 | `app/agent/blackboard.py` | `write_slot`、`read_slot`、`slot_summary` 等黑板读写函数 |
| 修改 | `app/agent/__init__.py` | 导出 `ConsultationState` 取代 `AgentState` |
| 新增 | `tests/agent/test_blackboard.py` | 单一写入者、只写不覆盖、多地块前缀的用例 |
| 删除 | `app/agent/nodes/` 中对 `AgentState` 的引用 | 仅替换类型标注，节点实现由 T2-14 起逐步取代 |

## 实现要点

### SlotValue

```python
class SlotValue(BaseModel):
    """单个信息槽位，带来源与状态。技术方案 5.3.1 原样定义。"""
    model_config = ConfigDict(extra="forbid")

    key: str
    value: str | None = None
    status: Literal["filled", "unknown", "missing"] = "missing"
    source: Literal["profile", "sensor", "weather", "user_answer", "image", "inferred"]
    confidence: float | None = None
    asked_at_round: int | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "SlotValue":
        if self.status == "filled" and self.value is None:
            raise ValueError("status=filled 时 value 不得为空")
        if self.status == "missing" and self.value is not None:
            raise ValueError("status=missing 时不得携带 value")
        return self
```

一致性校验器拦的是最隐蔽的一类错误：置了值却忘了改状态，或改了状态却没写值。这类不一致会直接导致 T2-17 的筛选器判断错误（把已答的槽位当成未问），而它不会报任何异常。

`source` 在契约原文中是必填 `Literal`，`status` 用 `Literal` 而不是 T2-01 的 `SlotStatus` 枚举，是为了与技术方案 5.3.1 的代码原文逐字一致；T2-01 的枚举值与之相同，序列化结果一致，二者不冲突。

### EvidenceItem

```python
class EvidenceItem(BaseModel):
    """一条证据，必须带溯源。技术方案 5.3.1 原样定义。"""
    model_config = ConfigDict(extra="forbid")

    knowledge_id: str
    title: str
    content: str
    process_id: str
    region_scope: str | None
    effective_date: date | None
    kind: Literal["pest", "disease", "pesticide", "cultivation", "threshold"]
    score: float
```

`region_scope` 记录实际命中的行政级别（县/市/省/全国），这是地区回退时必须保留的信息，供界面标注"当前按 XX 市通用农时提示"。

### ConsultationState

```python
class ConsultationState(TypedDict, total=False):
    # 身份与归属
    session_id: str
    user_id: str
    farm_plot_id: str | None
    # 流程
    process_id: ProcessId
    process_locked: bool
    config_version: int
    config_hash: str
    # 进展
    stage: ConsultStage
    round: int
    slot_round: int
    # 事实
    slots: dict[str, SlotValue]
    observations: dict
    evidence: list[dict]
    prescription_history: list[dict]
    # 产出
    diagnosis: dict | None
    prescription: dict | None
    gate_result: dict | None
    report_decision: dict | None
    # 会话
    messages: Annotated[list, add_messages]
    reply: str
    trace_id: str
```

`total=False` 是这个 TypedDict 的关键：LangGraph 允许节点只返回自己改动的键，未返回的键保留上一轮的值。若用 `total=True`，每个节点都必须返回全部字段，会让节点实现变得冗长且容易遗漏。

`config_version` 与 `config_hash` 在技术方案 5.3.1 的原文中没有列出，但 8.4 要求"在 `GateResult.checked_rules` 中记录版本号，使任何一次拦截都可追溯到配置版本"，而闸门只能从 state 读到它。因此这里加入两个字段，属于必要推断。

`prescription_history` 是档案中的用药史，与 `slots` 分开存放：前者是外部事实的只读快照，后者是本次问诊逐步累积的判断依据，二者的写入方与生命周期都不同。

### 黑板读写函数

```python
class BlackboardError(RuntimeError):
    """写入违反黑板规则时抛出。"""


def write_slot(
    state: ConsultationState,
    slot: SlotValue,
    config: ProcessConfig,
    *,
    writer: str,
    plot_prefix: str | None = None,
) -> None:
    """按黑板三条规则写入槽位。"""
    key = f"{plot_prefix}.{slot.key}" if plot_prefix else slot.key

    spec = config.slot_spec(slot.key)
    if spec is None:
        raise BlackboardError(f"槽位 {slot.key} 未在流程 {config.process_id.value} 中声明")
    if spec.owner != writer:
        raise BlackboardError(f"槽位 {slot.key} 的写入者为 {spec.owner}，{writer} 无权写入")

    existing = state.get("slots", {}).get(key)
    if existing is not None and existing.status == "filled":
        if SOURCE_PRIORITY[SlotSource(slot.source)] >= SOURCE_PRIORITY[SlotSource(existing.source)]:
            return                                    # 低优先级不得覆盖，静默保留原值
    state.setdefault("slots", {})[key] = SlotValue(key=key, **slot.model_dump(exclude={"key"}))
```

三条规则的执行点都在这里：`spec.owner != writer` 执行单一写入者；`SOURCE_PRIORITY` 比对执行只写不覆盖；`status` 的一致性由 `SlotValue` 的校验器执行。三者都是代码而非提示词约定。

低优先级写入**静默返回而不抛异常**是刻意选择：`inferred` 想覆盖 `profile` 是正常业务场景（系统按播种时间推算生育期，但档案里农户已手动修正过），这不是错误，只需忽略。若抛异常，正常路径也要写 `try`。

多地块前缀按技术方案 6.3 实现：`plot_prefix` 由调用方传入，当前问诊只针对一个地块，切换地块需开启新 `session_id`。

```python
def read_slot(state: ConsultationState, key: str, *, plot_prefix: str | None = None) -> SlotValue | None:
    full_key = f"{plot_prefix}.{key}" if plot_prefix else key
    return state.get("slots", {}).get(full_key)


def slot_summary(state: ConsultationState) -> dict[str, str]:
    """把槽位压成 键 → 展示值 的映射，供提示词注入。unknown 用固定文案。"""
    out: dict[str, str] = {}
    for key, slot in (state.get("slots") or {}).items():
        if slot.status == "filled":
            out[key] = slot.value or ""
        elif slot.status == "unknown":
            out[key] = "农户表示不清楚"
    return out
```

`slot_summary` 只输出 `filled` 与 `unknown`，跳过 `missing`。`unknown` 用固定文案而不是空串，是为了让模型知道"问过了但答不出"，从而降低确定性表述而不是继续追问。

### 事实与产出的读写

```python
def set_stage(state: ConsultationState, stage: ConsultStage) -> dict:
    return {"stage": stage}


def append_evidence(state: ConsultationState, items: list[EvidenceItem]) -> dict:
    merged = {e["knowledge_id"]: e for e in (state.get("evidence") or [])}
    for item in items:
        merged[item.knowledge_id] = item.model_dump(mode="json")
    return {"evidence": list(merged.values())}
```

`append_evidence` 以 `knowledge_id` 去重，避免同一轮重试导致证据重复累积，也避免 `EvidenceSet` 出现同一知识条目的多条副本从而干扰重排。

`ConsultationState` 中的 `evidence`、`diagnosis`、`prescription`、`gate_result`、`report_decision` 都是 `dict` 而非契约对象。原因是 TypedDict 交给 LangGraph 的 checkpointer 时需要可 JSON 序列化；契约对象在节点内部使用，进入 state 前统一 `model_dump(mode="json")`，从 state 取出时统一 `Model.model_validate(...)`。这个约定要在 `contracts/__init__.py` 里提供一对 `dump_contract` / `load_contract` 辅助函数。

### 与现有代码的迁移

现有 `AgentState` 的字段有两类去处：
- `session_id`、`user_id`、`messages`、`reply` 直接映射到 `ConsultationState` 同名字段
- `crop_tag` 由 `process_id` 取代；`symptoms`、`management`、`environment` 拆入 `slots`；`step` 由 `stage` 取代；`confidence_score` 从 state 移除（内部保留用于路由，见 T2-19）；`image_url` 与 `user_message` 移入 `observations` 与 `messages`

迁移期间 `app/agent/graph.py` 与 `app/agent/nodes/` 会编译失败，这是预期的：本项完成后主图由 T2-25 重建，节点由 T2-14 起逐个替换。为让 CI 在过渡期可用，`app/agent/graph.py` 在本项中标记为待删除并在 T2-25 移除，中间状态以 `# TODO(T2-25)` 注释保留。

## 验收标准

- `pytest tests/agent/test_blackboard.py` 全部通过，含：非 owner 写入抛 `BlackboardError`、`inferred` 覆盖 `profile` 被静默拒绝、`filled` 槽位同优先级不得覆盖、多地块键带 `plot_1.` 前缀
- `python -c "from app.agent.state import ConsultationState; print(sorted(ConsultationState.__annotations__))"` 输出的键集合与技术方案 5.3.1 一致，并含 `config_version` 与 `config_hash`
- `SlotValue(key='a', status='filled')` 抛 `ValidationError`，`SlotValue(key='a', status='missing', value='x', source='profile')` 抛 `ValidationError`
- `grep -rn "AgentState" backend/app/` 的结果仅为 `# TODO(T2-25)` 标注处，不再有类型标注引用
- `grep -rn "Step\." backend/app/agent/` 无结果
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
