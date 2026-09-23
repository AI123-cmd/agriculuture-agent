# T2-14 分诊智能体 Triage

## 这一项要做什么

在 `app/agent/agents/triage.py` 中实现分诊智能体，输出 `TriageResult`，决定本次问诊进入哪条流程、问诊对象是否已与农户确认、进入问诊前需要补齐哪些槽位。它是"受理即分流"的实现点：农户提问后第一个动作就是确定流程归属，而不是先收集症状再判断。

本智能体有一条硬约束：**不得生成任何问题**。它的唯一产出是流程标识与缺口列表。这条约束让它的输出可被单元测试完全覆盖，也让对象确认的交互由主图统一处理，而不是混在分诊里。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`FailureMode`、`AgentContext`
- 依赖 T2-03：必须已提供 `get_process_config`，用于读取各流程的 `object_aliases`
- 依赖 T2-09：必须已提供 `TriageResult`、`ObjectConfirmRequest`、`match_object_alias`
- 依赖 T2-55、T2-57：必须已提供模型网关与任务类型路由（`TaskType.TRIAGE` 对应小模型档位）
- 数据依赖：农户档案（`farm_entry.process_id`）与历史用药记录，由调用方以只读结构注入

## 参考章节

- 技术方案 第5章 5.3.2 分诊契约 TriageResult（含两条硬约束）
- 技术方案 第5章 5.3.2 对象确认规则（实体匹配与一次确认对话）
- 技术方案 第5章 5.1 智能体总表（①分诊，自治程度低，失败回退通用流程）
- 技术方案 第4章 4.4 模型分层策略（分诊用小模型，分类加槽位抽取，输出枚举）
- 技术方案 第6章 6.2 主管式确定性编排（`route_after_triage` 的三个分支）
- 技术方案 第12章 12.2 流程类型与档案模型调整（`object_type` 三值与"其他"类型的处理）
- 技术方案 第9章 9.1 流程配置模型（`object_aliases` 用于对象确认）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/triage.py` | `TriageAgent` 与其降级模板 |
| 新增 | `app/agent/agents/prompts/triage.py` | 分诊提示词构造（或落在 T2-60 的模板目录） |
| 新增 | `tests/agent/test_triage_agent.py` | 分流、对象确认、降级的三类用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `TriageAgent` |

## 实现要点

### 类骨架

```python

class TriageAgent(BaseAgent[TriageResult]):
    name = "triage"
    output_schema = TriageResult
    task_type = TaskType.TRIAGE
    failure_mode = FailureMode.DEGRADE          # 回退通用流程
    max_attempts = 2
    def __init__(self, gateway: ModelGateway, configs: dict[ProcessId, ProcessConfig]) -> None:
        super().__init__(gateway)
        self._configs = configs
```

`failure_mode` 为 `DEGRADE`，降级出口是 `process_id=general` 加 `fallback_reason`。这是技术方案 5.1 明确的"回退通用流程"。

### 输入构造

```python
    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        profile = state.get("profile") or {}
        system = [
            f"你是话农点金的分诊模块，唯一任务是把农户的问题归到七条流程之一。",
            f"可选流程及其识别词：{self._format_process_keywords()}",
            "你必须输出符合以下 JSON Schema 的内容：",
            json.dumps(TriageResult.model_json_schema(), ensure_ascii=False),
            "硬规则：不得生成任何问题。不得输出问句。",
            "若无法确定流程，选择 general 并在 fallback_reason 中说明原因。",
        ]
        user = [
            f"农户所在地区：{profile.get('location') or '未知'}",
            f"档案中的经营条目：{self._format_entries(profile.get('entries'))}",
            f"农户本次输入：{self._current_message_text(state)}",
        ]
        return [{"role": "system", "content": "\n".join(system)},
                {"role": "user", "content": "\n".join(user)}]
```

系统提示中显式写出"不得输出问句"是第二道防线，第一道是 `TriageResult` 契约里根本没有问题字段。技术方案 5.3.2 对这条约束的表述是"这保证了受理即分流的纯粹性，也让它可被单元测试完全覆盖"。

`_format_process_keywords` 从每条流程配置的 `object_aliases` 与 `display_name` 拼装，而不是硬编码关键词表。这样新增流程只需加 YAML，分诊提示词自动包含新流程的识别词。这是"流程配置是唯一事实来源"在提示词层的落实。

### 提示词中的可选流程清单

```python
    def _format_process_keywords(self) -> str:
        lines = []
        for pid, config in self._configs.items():
            aliases = "、".join(config.object_aliases[:8]) or "无"
            lines.append(f"{pid.value}（{config.display_name}）：{aliases}")
        return "\n".join(lines)
```

别名只取前八个。技术方案 10.4 指出长上下文会降低指令遵循度，而流程别名是长列表中最不具区分度的部分。截断到八个既能覆盖主要表述，也不至于让提示词膨胀。

### 对象确认的代码侧判定

对象确认**不由模型决定**，模型只负责抽取农户表述中的作物实体，判定由纯函数完成：

```python
    def resolve_object_confirmation(
        self,
        triage: TriageResult,
        state: ConsultationState,
    ) -> ObjectConfirmRequest | None:
        """对象确认判定：档案流程与农户表述做实体匹配。"""
        profile_pid = state.get("profile_process_id")
        text = self._current_message_text(state)
        matched = match_object_alias(text, self._configs)
        if profile_pid is None:
            return None                                    # 档案为空，不做确认，直接按分诊结果走
        if matched is None or matched == profile_pid:
            return ObjectConfirmRequest(
                asked_process_id=triage.process_id,
                profile_process_id=ProcessId(profile_pid),
                candidate_labels=[label_of(profile_pid), label_of(triage.process_id)],
                question_text=f"您档案里登记的是{display_of(profile_pid)}，这次问的是{display_of(triage.process_id)}吗",
                matched_alias=None,
            )
        return None
```

技术方案 5.3.2 的规则是："档案为空或农户问的对象与档案不符，系统需先与农户确认对象"。因此档案为空时**不确认**（没有可对比的对象，问也无从问起），档案非空且不匹配时返回确认请求，匹配时直接通过。确认后更新 `process_id` 并锁定，这个更新由主图在确认交互节点完成。

`ObjectConfirmRequest` 的生成是纯代码逻辑，模型不参与。若让模型生成确认问句，会出现"您问的是茶树还是水稻呢"这类把两个对象并列而不说明档案里是什么的表述，农户无从回答。

### `object_confirmed` 的来源

`TriageResult.object_confirmed` 由模型给出初步值，但**最终值由代码覆写**：

```python
    async def post_validate(self, result: TriageResult, state: ConsultationState, ctx: AgentContext) -> TriageResult:
        confirm = self.resolve_object_confirmation(result, state)
        if confirm is not None:
            result = result.model_copy(update={"object_confirmed": False})
        if result.process_id is ProcessId.GENERAL and not result.fallback_reason:
            result = result.model_copy(update={"fallback_reason": "未匹配到流程专属信号"})
        return result
```

覆写而不是信任模型，是因为 `object_confirmed` 直接决定主图是否插入确认交互。模型若因为看到了农户输入中的作物词就置 True，会跳过本应发生的确认，而档案与表述不符恰恰是最需要确认的场景。

`fallback_reason` 的兜底补填是防止通用流程的降级原因缺失。契约已经校验它非空，这里的补填针对的是"模型给了值但值为空串"的情况。

### 槽位缺口列表

```python
    def compute_slot_gaps(self, state: ConsultationState, config: ProcessConfig) -> list[str]:
        """按流程必问清单与现有槽位，算出进入问诊前的缺口。纯代码，不用模型。"""
        gaps: list[str] = []
        for spec in sorted(config.required_slots, key=lambda s: s.ask_order):
            if not spec.required:
                continue
            slot = state.get("slots", {}).get(spec.key)
            if slot is None or slot.status == "missing":
                if spec.source_hint:
                    continue          # 可由档案或环境自动填充，不算缺口
                gaps.append(spec.key)
        return gaps
```

`slot_gaps` 也由代码计算而非模型生成。原因是缺口是"必问清单减去已确认减去可自动填充"的集合运算，规则明确，交给模型只会引入不确定性。`source_hint` 非空的槽位不算缺口，这一点与 T2-17 的筛选器第二步口径一致。

`post_validate` 中用 `compute_slot_gaps` 的结果覆盖模型的 `slot_gaps`。

### 降级实现

```python
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> TriageResult:
        """降级：回退通用流程，并保留档案中的对象标签。"""
        profile = state.get("profile") or {}
        return TriageResult(
            process_id=ProcessId.GENERAL,
            object_confirmed=True,                 # 通用流程无需确认对象
            object_label=profile.get("object_label") or "未指定作物",
            slot_gaps=[],
            profile_ready=bool(profile),
            fallback_reason=f"分诊服务不可用，已按通用农业流程继续（{reason}）",
            handoff_to_report=False,
        )
```

降级的 `object_confirmed=True` 是刻意的：已经无法分诊的情况下再插入一次对象确认交互，对农户是纯粹的额外负担，而通用流程本身不依赖对象精度。

降级的 `fallback_reason` 里带上 `reason`，让线上能区分"模型说没法归类"与"模型不可用"两种 general 占比。

## 验收标准

- `pytest tests/agent/test_triage_agent.py` 全部通过，含三类用例：输入"我家茶树叶子发黄"分流到 `tea`、档案为水稻而农户问茶时 `object_confirmed` 被覆写为 False、网关失败时降级为 `general` 且 `fallback_reason` 非空
- 断言 `TriageResult.model_fields` 中不存在任何承载问题的字段（键名不含 `question`、`prompt`、`ask`）
- 断言 `resolve_object_confirmation` 在档案为空时返回 `None`
- 断言 `compute_slot_gaps` 对 `source_hint` 非空的槽位不纳入缺口
- 断言在 `USE_MOCK_LLM=true` 下 `run` 返回的对象 `process_id` 为 `ProcessId` 枚举实例而非字符串
- 断言系统提示内容包含全部七个 `process_id` 字符串
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
