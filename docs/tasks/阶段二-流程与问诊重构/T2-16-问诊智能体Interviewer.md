# T2-16 问诊智能体 Interviewer

## 这一项要做什么

在 `app/agent/agents/interviewer.py` 中实现问诊智能体，输出 `InterviewBatch`，每轮生成一到两个追问。它要做四件事：把候选池里的问题方向说得像人话、为每个问题解释为什么问、按轮次计算进度提示、在数据缺失时给出显式标注。 本智能体的关键设计是"模型只在给定范围内选择问题方向"。候选池由 T2-17 的筛选器在调用模型之前用纯代码算好，模型不得超出候选池生成新问题。这让"跳过已知项"与"不漏必问项"两个要求由代码保证，模型只负责语言表达。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`AgentContext`；依赖 T2-17：必须已提供 `select_candidate_slots` 与候选池结构
- 依赖 T2-10：必须已提供 `InterviewBatch`、`InterviewQuestion`；依赖 T2-15：必须已提供 `ObservationResult`（`env_source` 决定是否纳入 `offline_questions`）
- 依赖 T2-02：必须已提供 `ProcessConfig` 的 `role_definition`、`terminology`、`redlines`、`confusable_items`；依赖 T2-55、T2-57：必须已提供网关与任务路由（`TaskType.INTERVIEW` 对应主模型档位）

## 参考章节

- 技术方案 第5章 5.3.3 问诊契约 InterviewBatch（五条硬约束对照表）；技术方案 第5章 5.3.3 追问候选池的构造（三步筛选，top-6）
- 技术方案 第9章 9.4 提示词组织（`build_interviewer_prompt` 的七段结构与关键设计）；技术方案 第4章 4.4 模型分层策略（问诊用主模型，直接影响体验与信息补全率）
- 技术方案 第7章 7.3 状态机推进规则（单槽位重复追问上限与 unknown 强制写入）；技术方案 第10章 10.3 降级链路（问诊降级为模板问题）
- 技术方案 第5章 5.1 智能体总表（③问诊自治程度中，失败降级为模板问题）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/interviewer.py` | `InterviewerAgent` 与模板问题降级 |
| 新增 | `app/agent/agents/prompts/interviewer.py` | `build_interviewer_prompt` 的七段拼装 |
| 新增 | `tests/agent/test_interviewer_agent.py` | 候选域约束、进度提示、模板降级的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `InterviewerAgent` |

## 实现要点

### 类骨架

```python
class InterviewerAgent(BaseAgent[InterviewBatch]):
    name = "interviewer"
    output_schema = InterviewBatch
    task_type = TaskType.INTERVIEW
    failure_mode = FailureMode.DEGRADE          # 降级为模板问题
    max_attempts = 2
    def __init__(self, gateway: ModelGateway, budget: ConsultBudget) -> None:
        super().__init__(gateway)
        self._budget = budget
```

### 七段提示词拼装

按技术方案 9.4 的结构逐段拼装，各段来源明确：

```python
def build_interviewer_prompt(config: ProcessConfig, state: ConsultationState, ctx: AgentContext,
    candidates: CandidatePool, ) -> list[dict]:
    system = [
        f"你是{config.role_definition}。",                                        # 1 角色定位（按流程）
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(InterviewBatch.model_json_schema(), ensure_ascii=False)}",
        "每轮最多问 2 个问题。每个问题必须说明为什么问。",                            # 3 硬规则（共用）
        "不得询问已经知道答案的信息。",
        f"使用以下术语：{format_terms(config.terminology.display_metrics)}",        # 4 术语规范（按流程）
        f"面向农户的表述必须口语化：{format_colloquial(config.terminology.colloquial_map)}",
        f"严禁输出：{format_redlines(config.redlines)}",                            # 5 禁用输出（按流程红线）
        f"你必须主动追问下列鉴别点：{format_confusables(config.confusable_items)}",  # 6 易混淆项（按流程）
    ]
    user = [                                                                       # 7 候选问题池（代码筛选）
        f"已确认信息：{format_slots(state['slots'])}",
        f"当前环境事实：{format_observations(state.get('observations'))}",
        f"候选追问方向（从中选择，不得超出）：{format_candidates(candidates)}",
    ]
    return [{"role": "system", "content": "\n".join(system)}, {"role": "user", "content": "\n".join(user)}]
```

结构与方法名与技术方案 9.4 的代码原文一致，只增加 `ctx` 参数以便注入 `trace_id` 与地区。第 6 段从 `confusable_items` 生成鉴别点，实现"一份配置驱动三处行为"中的问诊这一处。

### 候选池的强约束

```python
    def validate_output(self, result: InterviewBatch, state: ConsultationState, ctx: AgentContext) -> None:
        """契约之外的校验：问题方向必须落在候选池内。"""
        pool = state.get("_candidate_pool_keys") or set()
        outside = [q.slot_key for q in result.questions if q.slot_key not in pool]
        if outside:
            raise ContractViolation(f"问了候选池之外的方向: {outside}")
```

这条校验是"模型只能在给定范围内选择"的执行点。它在 `validate_output` 里抛 `ContractViolation`，从而触发 T2-13 基类的重试（带修复提示）。重试仍失败则走模板降级。 候选池由节点在调用前算好并放入一个临时键（下划线开头，表示不作为状态契约的一部分）。这样校验器不需要重新计算，也不必依赖 `AgentContext` 携带流程外的运行数据。

### 进度提示由代码计算

```python
    def compute_stage_hint(self, state: ConsultationState) -> str:
        """进度提示：由代码按轮次计算，非模型生成。"""
        current = min(state.get("round", 0) + 1, self._budget.max_interview_rounds)
        total = self._budget.max_interview_rounds
        return f"正在了解情况 {current}/{total}"
```

技术方案 5.3.3 的对照表明确"每步要有进度提示，由代码按轮次计算，非模型生成"。因此在 `post_validate` 中用它覆盖模型输出的 `stage_hint`：

```python
    async def post_validate(self, result: InterviewBatch, state: ConsultationState, ctx: AgentContext) -> InterviewBatch:
        updates: dict[str, Any] = {"stage_hint": self.compute_stage_hint(state)}
        note = self.compute_confidence_note(state)
        if note:
            updates["confidence_note"] = note
        result = result.model_copy(update=updates)
        self._mark_asked_slots(state, result)
        return result
```

进度提示的文案是固定的模板串，不由模型改写。理由是这个文案的准确性直接决定农户对"还要问几轮"的预期，模型改写可能引入"大概再问两三个问题"这类不准确的表述。

### 数据缺失标注

```python
    def compute_confidence_note(self, state: ConsultationState) -> str | None:
        observations = state.get("observations") or {}
        env_source = observations.get("env_source")
        if env_source == "none":
            return "暂时没有环境数据，下面的判断主要根据您描述的情况。"
        if env_source == "sensor_stale":
            return "传感器数据不是最新的，判断可能有偏差。"
        unknown_slots = [k for k, v in (state.get("slots") or {}).items() if v.status == "unknown"]
        if len(unknown_slots) >= 3:
            return "有几项您不太确定，我会把把握程度说清楚。"
        return None
```

`unknown` 槽位累计到三个时给出提示，是"未知是合法答案"在产品层面的体现：系统不逼问，但要主动说明"因为不知道这些，结论的把握会低一些"。这个阈值（三个）是工程经验值，可由执行者调整，调整时同步更新测试。

### 单槽位追问上限

```python
    def _mark_asked_slots(self, state: ConsultationState, batch: InterviewBatch) -> None:
        """记录本轮的追问记录；超限槽位将在写入时强制置 unknown。"""
        counters = dict(state.get("_ask_counters") or {})
        for q in batch.questions:
            counters[q.slot_key] = counters.get(q.slot_key, 0) + 1
        evictions = [k for k, v in counters.items() if v > self._budget.max_slot_rounds]
        if evictions:
            log.info("slot ask limit hit", extra={"slots": evictions})
```

技术方案 7.3 规定"单槽位被追问超过 2 次仍未填，强制写 `status='unknown'`，不再追问"，对应 `max_slot_rounds` 预算项。强制写入的动作发生在槽位写入侧（T2-08 的 `write_slot` 配合预算检查），本智能体只负责计数。这条落实产品说明书 4.2.2 的"不允许为了凑齐信息而反复逼问同一个问题"。 被标记为超限的槽位必须从下一轮的候选池中剔除，这个剔除由 T2-17 的筛选器第三步读取 `_ask_counters` 完成。

### 跳过项与 unknown

用户选择跳过项时，写入 `SlotValue(status="unknown", source="user_answer")`。这一步不在本智能体里做（本智能体只生成问题），但要在提示词的第 3 段硬规则中明确"用户表示不清楚时不要换个说法再问一遍"，因为模型看到 `unknown` 槽位时容易试图换个措辞再问。

### 模板问题降级

```python
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> InterviewBatch:
        """降级：按候选池顺序取前两个必问槽位，用流程配置里的文案直接提问。"""
        config = ctx.process_config
        pool = self._template_pool(state, config)
        questions = [InterviewQuestion(slot_key=spec.key, text=spec.display_label,
            why=TEMPLATE_WHY.get(spec.group.value, "了解基本情况才能给准建议"), options=spec.options,
            answer_type="single" if spec.options else "text", ) for spec in pool[:2] ]
        if not questions:
            raise AgentDegraded("无可用模板问题，应由主图推进到下一环节")
        return InterviewBatch(questions=questions, stage_hint=self.compute_stage_hint(state),
            confidence_note="网络有点忙，先用固定问题问您几个关键的。", )
    def _template_pool(self, state: ConsultationState, config: ProcessConfig) -> list[SlotSpec]:
        """模板问题来源：必问且未填且未被超限剔除的槽位，按 ask_order 排序。"""
        counters = state.get("_ask_counters") or {}
        out = []
        for spec in sorted(config.required_slots, key=lambda s: s.ask_order):
            slot = (state.get("slots") or {}).get(spec.key)
            filled_or_answered = slot is not None and slot.status in ("filled", "unknown")
            if spec.required and not filled_or_answered and counters.get(spec.key, 0) <= self._budget.max_slot_rounds:
                out.append(spec)
        return out
```

模板降级必须能独立工作，因为它的用途是"模型不可用时仍能把问诊走完"。技术方案 10.3 强调"模板必须本身可读，不能假设后续一定有模型润色"，因此 `text` 直接用 `display_label`（已经过配置作者的口语化处理），不在降级路径上做任何语言加工。 `TEMPLATE_WHY` 是一张按 `SlotGroup` 组织的固定说明表：

```python
TEMPLATE_WHY: dict[str, str] = {"病虫情": "看清症状才能判断是哪一类问题", "管理": "近期农事与用药会影响判断", "种情": "播种和生育期决定当前该防什么",
    "肥情": "施肥情况会影响长势与抗性", "墒情": "土壤干湿关系到病害发生条件", }
```

### 无环境数据时的增强追问

```python
    def include_offline_questions(self, state: ConsultationState, config: ProcessConfig) -> list[SlotSpec]:
        """env_source 为 none 时把 offline_questions 纳入候选池。"""
        observations = state.get("observations") or {}
        if observations.get("env_source") != "none":
            return []
        specs: list[SlotSpec] = []
        for oq in config.offline_questions:
            if (spec := config.slot_spec(oq.slot_key)) is not None:
                specs.append(spec)
        return specs
```

这些规格交给 T2-17 的筛选器与常规必问槽位合并后排序，本智能体不单独处理。这样"无传感器时的增强追问"与"必问清单"走同一条筛选链路，不会出现两套逻辑。

## 验收标准

- `pytest tests/agent/test_interviewer_agent.py` 全部通过，含三类用例：模型输出候选池外的 `slot_key` 时抛 `ContractViolation` 并重试、`stage_hint` 被代码覆写为"正在了解情况 N/4"、模型不可用时降级返回模板问题；断言降级时返回的 `questions` 长度不超过 2，且每项的 `text` 等于对应槽位的 `display_label`
- 断言 `compute_stage_hint` 在 `round=0` 时输出 `"正在了解情况 1/4"`，在 `round=9` 时输出 `"正在了解情况 4/4"`（不越界）；断言 `env_source="none"` 时 `confidence_note` 非空，`env_source="sensor"` 且无 unknown 槽位时为 `None`
- 断言系统提示内容包含流程的 `role_definition`、`display_metrics`、`colloquial_map` 与至少一个 `confusable_items` 候选；断言 `_template_pool` 剔除 `status` 为 `filled` 或 `unknown` 的槽位，并剔除追问次数超过 `max_slot_rounds` 的槽位
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
