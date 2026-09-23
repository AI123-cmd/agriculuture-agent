# T2-19 判定智能体 Diagnoser

## 这一项要做什么

在 `app/agent/agents/diagnoser.py` 中实现判定智能体，综合证据集与槽位输出 `DiagnosisResult`。它是全链路自治程度最高的环节（技术方案 5.1 标注为"高（推理）"），也是准确性的核心：鉴别推理与易混淆项排除都在这里完成。

本智能体有三个必须由代码保证的行为：把握程度只输出枚举而不是分数、对有易混淆项的流程强制输出排除项、结论不确定时给出不确定性来源或待补信息。三者中前两条依赖流程配置，第三条依赖契约校验。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`AgentContext`
- 依赖 T2-11：必须已提供 `DiagnosisResult`
- 依赖 T2-18：必须已提供 `EvidenceSet`，空集时须走不确定路径
- 依赖 T2-02：必须已提供 `ProcessConfig` 的 `confusable_items` 与 `terminology`
- 依赖 T2-23：转人工上下文包由本项在超边界时调用（反向依赖，本项只产出触发信号）
- 依赖 T2-55、T2-57：必须已提供网关与任务路由（`TaskType.DIAGNOSE` 对应主模型档位）

## 参考章节

- 技术方案 第5章 5.3.4 判定契约 DiagnosisResult（三条硬约束）
- 技术方案 第5章 5.3.4 `excluded` 必填且非空（对有易混淆项的流程）
- 技术方案 第5章 5.3.4 `out_of_scope = True` 时跳过处方环节直达转人工
- 技术方案 第9章 9.1 `confusable_items` 直接生成判定契约的 excluded 字段
- 技术方案 第6章 6.4 受限并行会诊（`force_uncertain` 的意义）
- 技术方案 第4章 4.4 模型分层策略（判定用主模型，准确性的核心环节）
- 技术方案 第6章 6.7 转人工升级（四类情形中的超出能力边界）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/diagnoser.py` | `DiagnoserAgent` 与排除项校验 |
| 新增 | `app/agent/agents/prompts/diagnoser.py` | 判定提示词构造 |
| 新增 | `tests/agent/test_diagnoser_agent.py` | 排除项、空证据、超边界的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `DiagnoserAgent` |

## 实现要点

### 类骨架

```python
class DiagnoserAgent(BaseAgent[DiagnosisResult]):
    name = "diagnoser"
    output_schema = DiagnosisResult
    task_type = TaskType.DIAGNOSE
    failure_mode = FailureMode.DEGRADE          # 输出不确定加求助引导
    max_attempts = 2

    def __init__(self, gateway: ModelGateway, budget: ConsultBudget) -> None:
        super().__init__(gateway)
        self._budget = budget
```

判定降级为"输出不确定加求助引导"，与技术方案 5.1 与 10.3 一致。判定不重试太多次：`max_attempts = 2` 与基类默认一致，因为判定的每次重试都要跑主模型，成本最高。

### 排除项的业务校验

```python
    def required_exclusions(self, config: ProcessConfig) -> set[str]:
        """从易混淆项生成必须显式排除的候选集合。"""
        required: set[str] = set()
        for pair in config.confusable_items:
            if pair.must_exclude:
                required.update(pair.candidates)
        return required

    def validate_output(self, result: DiagnosisResult, state: ConsultationState, ctx: AgentContext) -> None:
        """业务校验：有易混淆项的流程必须显式排除。"""
        required = self.required_exclusions(ctx.process_config)
        if not required:
            return
        if not result.excluded:
            raise ContractViolation("该流程有易混淆项，必须输出 excluded")
        missing = required - set(result.excluded) - {result.conclusion}
        if missing:
            raise ContractViolation(f"未显式排除: {sorted(missing)}")
```

`missing` 的计算里减去了 `result.conclusion`：如果结论本身就是某个候选（如结论是"青枯病"），它不需要出现在排除项里，但另一个候选必须出现。技术方案 5.3.4 的表述是"强制输出排除项，使模型必须做鉴别而不是直接匹配"，因此校验的重点是"另一个候选有没有被排除"，而不是"所有候选都要出现在排除列表"。

这条校验失败会触发重试（带修复提示），重试仍失败则降级为不确定结论。降级为不确定比输出一个未做鉴别的确定结论更安全：辣椒流程的青枯病与枯萎病处置方向完全不同，猜错的代价是无效用药。

### 不确定结论的补全

```python
    async def post_validate(self, result: DiagnosisResult, state: ConsultationState, ctx: AgentContext) -> DiagnosisResult:
        updates: dict[str, Any] = {}

        # 1. 术语档位由代码填入，不由模型自由生成
        updates["terminology_profile"] = ctx.process_config.process_id.value

        # 2. 证据集为空时强制不确定
        evidence = state.get("evidence") or []
        if not evidence and result.confidence_level != "uncertain":
            updates["confidence_level"] = "uncertain"
            updates.setdefault("uncertainty_source", []).append("未检索到支撑条目")

        # 3. 面板未收敛时强制不确定（由 T2-20 写入 state 标记）
        if state.get("panel_force_uncertain"):
            updates["confidence_level"] = "uncertain"
            updates.setdefault("uncertainty_source", []).append("并行会诊未能唯一确定")

        # 4. 超边界时补齐原因
        if result.out_of_scope and not result.boundary_reason:
            updates["boundary_reason"] = "该问题超出本流程的能力范围"

        merged = {**result.model_dump(), **updates}
        if merged["confidence_level"] == "uncertain" and not merged["needs_more_info"] and not merged["uncertainty_source"]:
            merged["needs_more_info"] = ["建议补充症状照片或到农技站咨询"]
        return DiagnosisResult.model_validate(merged)
```

`terminology_profile` 由代码从 `process_id` 填入而不是让模型生成。技术方案 5.3.4 的注释说明它是"命中的流程术语档，如 tea"。既然问诊已经在茶流程内进行，这个值就是确定的，让模型生成只会引入不一致。

三条强制不确定的来源都记入 `uncertainty_source`：无证据、面板未收敛、以及模型自身判断不确定。三者分开记录，让线上能区分"知识库覆盖不足"与"真难判断"，前者要补知识，后者要补判据。

### 内部置信度分数与展示的分离

```python
    def internal_confidence(self, result: DiagnosisResult, evidence_count: int) -> float:
        """内部置信度，仅供路由判断。不得进入任何契约字段或展示文案。"""
        base = {"likely": 0.85, "preliminary": 0.6, "uncertain": 0.3}[result.confidence_level]
        evidence_bonus = min(0.1, 0.02 * evidence_count)
        return min(1.0, base + evidence_bonus)
```

这个方法返回的分数写入 `state["confidence_score"]` 供 T2-24 的条件边使用，**不写入 `DiagnosisResult`**。技术方案 5.3.4 明确"内部可以保留置信度分数用于路由判断，但不得进入展示文案"。在契约类本身不含分数字段（T2-11 已保证）的基础上，这里再把分数的产生限制在一个方法内，便于审计"哪些地方读了分数"。

### 需补信息触发回环

```python
    def should_loop_back(self, result: DiagnosisResult, state: ConsultationState) -> bool:
        """是否需要回到问诊补信息。受预算约束。"""
        if result.out_of_scope:
            return False
        if not result.needs_more_info:
            return False
        loops = state.get("judge_loops", 0)
        rounds = state.get("round", 0)
        return loops < self._budget.max_judge_loops and rounds < self._budget.max_interview_rounds
```

两个上限都要检查：`max_judge_loops` 限制"置信度不足反向补问最多 2 次"，`max_interview_rounds` 限制总轮次不超过 4。只检查一个会导致另一条约束失效：若反复补问但每轮都算作一次问诊轮次，轮次上限就够了；但若补问不消耗问诊轮次（在同一轮内多次取证），就需要 `max_judge_loops` 兜底。两个都检查的原因是它们由不同机制维护，不能假设一个蕴含另一个。

### 超边界的处置

```python
    def build_boundary_notice(self, result: DiagnosisResult, config: ProcessConfig) -> str:
        """超边界时的面向农户文案：建议咨询农技站加可自行观察的要点。"""
        points = result.basis[:2] or ["记录症状出现时间与范围变化"]
        observations = "；".join(points)
        return (
            f"这个问题超出了我能给准建议的范围。建议带上这条信息去当地农技站：{observations}。"
            f"您也可以先观察{observations}，有明显变化时再来问我。"
        )
```

技术方案 6.7 要求超边界时输出"建议咨询当地农技站 + 可自行观察的要点"。"可自行观察的要点"从 `basis` 取前两条，因为 `basis` 是模型已经逐条列出的判断依据，它本身就是观察项的来源。这样文案的实质内容有出处，而不是重新生成一段泛泛的建议。

`out_of_scope=True` 时主图跳过处方直达转人工，这个路由由 `route_after_judge`（T2-24）实现，本智能体只负责正确置位这个字段。

### 提示词构造

```python
def build_diagnoser_prompt(
    config: ProcessConfig,
    state: ConsultationState,
    evidence: list[dict],
) -> list[dict]:
    system = [
        f"你是{config.role_definition}，负责给出判断结论。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(DiagnosisResult.model_json_schema(), ensure_ascii=False)}",
        "判断依据必须逐条列出，每条对应证据中的具体条目。",
        "把握程度只能取 likely、preliminary、uncertain 三者之一，不得输出百分比或分数。",
        "下列候选必须显式鉴别，不能只给结论：",
        format_confusables(config.confusable_items),
        f"使用术语：{format_terms(config.terminology.display_metrics)}",
        f"严禁输出：{format_redlines(config.redlines)}",
        "证据不足以支撑结论时，选择 uncertain 并说明需要补什么，不要用模糊表述掩盖。",
    ]
    user = [
        f"已确认信息：{format_slots(state['slots'])}",
        f"现场事实：{format_observations(state.get('observations'))}",
        f"检索到的证据：{format_evidence(evidence)}",
        f"档案用药史：{format_history(state.get('prescription_history'))}",
    ]
    return [{"role": "system", "content": "\n".join(system)},
            {"role": "user", "content": "\n".join(user)}]
```

提示词中显式禁止百分比，与契约层不含分数字段形成双保险。技术方案 5.3.4 指出"把握程度必须用农户听得懂的话表达，不能显示百分比分数"，而模型在自由生成时很容易写出"大约 70% 的可能是"，因此这条禁令必须在提示词里也出现一次。

`format_evidence` 传入的是证据的序列化字典（来自 `state["evidence"]`），只注入相关条目的标题与内容摘要，不注入全部。技术方案 10.4 的上下文裁剪要求"只注入相关槽位与证据"。

### 降级实现

```python
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> DiagnosisResult:
        return DiagnosisResult(
            conclusion="暂时无法给出明确结论",
            confidence_level="uncertain",
            basis=["判定服务暂时不可用"],
            uncertainty_source=[f"判定环节不可用（{reason}）"],
            excluded=list(self.required_exclusions(ctx.process_config)),
            terminology_profile=ctx.process_config.process_id.value,
            needs_more_info=["建议先观察植株变化，或携带症状照片到农技站咨询"],
            out_of_scope=False,
            boundary_reason=None,
        )
```

降级的 `excluded` 用 `required_exclusions` 的完整集合填充，而不是留空。理由是这个集合是配置给定的、与本次问诊内容无关的固定值，填上不构成任何编造。若留空，契约的业务校验会再次失败，形成降级后又降级的循环。

`out_of_scope=False` 是刻意的：判定服务不可用不等于问题超边界。超边界是一个关于问题内容的判断，服务不可用时无从判断，因此按"不超边界"处理并输出不确定结论。这样农户拿到的是一条"暂时给不出明确结论，建议观察或求助"的回复，而不是被直接推到人工通道。

## 验收标准

- `pytest tests/agent/test_diagnoser_agent.py` 全部通过，含三类用例：辣椒流程输出 `excluded` 缺一个候选时抛 `ContractViolation` 并重试、证据集为空时 `confidence_level` 被覆写为 `uncertain`、模型不可用时降级且 `excluded` 等于配置要求的完整集合
- 断言 `DiagnosisResult.model_fields` 中不含任何名称含 `score` 或 `percent` 的字段
- 断言 `internal_confidence` 的返回值不被写入 `DiagnosisResult`（检查 `result.model_dump()` 的键集合）
- 断言 `terminology_profile` 恒等于 `process_config.process_id.value`
- 断言 `panel_force_uncertain` 为 True 时 `confidence_level` 被覆写为 `uncertain` 且 `uncertainty_source` 含"并行会诊"
- 断言 `should_loop_back` 在 `judge_loops` 达到 `max_judge_loops` 时返回 False，在 `round` 达到 `max_interview_rounds` 时返回 False
- 断言 `out_of_scope=True` 时 `should_loop_back` 返回 False
- 断言 `build_boundary_notice` 的输出包含 `basis` 中的至少一条内容
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
