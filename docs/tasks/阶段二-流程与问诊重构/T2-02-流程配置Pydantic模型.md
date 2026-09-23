# T2-02 流程配置 Pydantic 模型

## 这一项要做什么

在 `app/process_config/models.py` 中定义流程配置的完整 Pydantic v2 模型族，把技术方案 9.1 给出的 `ProcessConfig`、`SlotSpec`、`TerminologySpec`、`ConfusablePair` 以及方案中引用但未展开的 `RedlineSpec`、`TimingRule`、`OutputTemplateSpec`、`FollowupPolicy`、`OfflineQuestion` 全部落到代码上。

流程配置是全项目的事实来源，它同时被六处消费：分诊读取 `object_aliases`，问诊读取 `required_slots` 与 `terminology`，判定读取 `confusable_items`，处方读取 `output_template`，闸门读取 `redlines`，提示读取 `timing_rules` 与 `calendar_source`。把这些字段先用模型固化，后续七份 YAML 才有统一的校验标准，任何一处写错都会在加载阶段而不是运行阶段暴露。

## 前置依赖

- 依赖 T2-01：必须已提供 `ProcessId`、`SlotGroup`、`ViolationSeverity`、`AnswerType`、`SlotSource`
- 外部环境：pydantic 2.x，PyYAML（仅 T2-03 需要）
- 与 T2-06 的边界：红线相关模型本项只定义 `RedlineSpec` 这一层引用外壳，完整红线语义（数据载荷结构、完整性校验规则）在 T2-06 展开

## 参考章节

- 技术方案 第9章 9.1 流程配置模型（`ProcessConfig` 与 `SlotSpec` 字段全貌）
- 技术方案 第9章 9.1 术语表的结构（`TerminologySpec` 三字段）
- 技术方案 第9章 9.1 `confusable_items` 直接生成判定契约的 excluded 字段（`ConfusablePair`）
- 技术方案 第7章 7.4 七条流程的子图（流程间差异点清单，逐项对应配置字段）
- 技术方案 第8章 8.4 流程红线配置化（红线 YAML 形态与 `data` 字段）
- 技术方案 第11章 回访（`FollowupPlan` / `FollowupTask` 的取值来源）
- 附录 B：流程配置样例（茶树配置的字段命名与嵌套形态）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/process_config/__init__.py` | 包初始化，导出模型 |
| 新增 | `app/process_config/models.py` | 全部流程配置模型 |
| 新增 | `tests/process_config/test_models.py` | 模型校验的正例与反例 |
| 修改 | `requirements.txt` | 若 T1-01 未含 `pydantic-settings`，此处确认 pydantic 版本为 2.x |

## 实现要点

### 外壳模型 ProcessConfig

```python
class ProcessConfig(BaseModel):
    """一条流程的完整配置。加载成功即视为该流程可路由。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    process_id: ProcessId
    version: int
    display_name: str
    role_definition: str
    required_slots: list[SlotSpec]
    redlines: list[RedlineSpec]
    terminology: TerminologySpec
    timing_rules: list[TimingRule]
    output_template: OutputTemplateSpec
    calendar_source: str | None
    followup_policy: FollowupPolicy
    offline_questions: list[OfflineQuestion]
    panel_enabled: bool = False
    object_aliases: list[str] = []
    confusable_items: list[ConfusablePair]
    banned_topics: list[str] = []
```

`extra="forbid"` 是关键：YAML 里写错字段名必须立即报错，而不是被安静忽略。历史上这类"配置写了但没生效"的问题最难排查，因为它既不报错也不生效。`frozen=True` 使配置对象不可变，避免某个智能体在处理中途改到配置，从而让"同一次问诊内配置一致"成为结构性保证。

`version` 用整数单调递增，与数据库表 `process_config.config_version` 对齐；`display_name` 是给农户看的流程名（"茶树"而非 "tea"）。

### 槽位规格 SlotSpec

```python
class SlotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    display_label: str
    group: SlotGroup
    required: bool = True
    owner: str
    source_hint: list[str] = []
    options: list[str] | None = None
    ask_order: int = 0
```

字段语义与技术方案 9.1 一致：`label` 是内部用语（"叶发病率"），`display_label` 是农户看的口语化标签（"多少叶子有问题"）。`owner` 声明唯一有权写入该槽位的智能体，对应技术方案 6.3 的"单一写入者"规则。`source_hint` 列出可自动填充的来源（如 `["profile"]` 表示档案里已有则不必问），T2-17 的筛选器第二步据此剔除候选。`ask_order` 越小越先问，同一批次内的问题按此排序。

### 术语表 TerminologySpec

```python
class TerminologySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    internal_terms: dict[str, str]
    colloquial_map: dict[str, str]
    display_metrics: list[str]
```

两套术语必须分开。`internal_terms` 给模型与知识库检索用，`colloquial_map` 给展示层做替换。展示层渲染时必须过 `colloquial_map`，例如"墒情"到"地里的干湿"、"生育期"到"庄稼长到哪个阶段"、"置信度"到"有多大把握"。`display_metrics` 是本流程优先使用的指标口径，茶流程用"叶发病率"，水稻用"病丛率/百丛虫量"，杂草用"经济阈值"。

### 易混淆项 ConfusablePair

```python
class ConfusablePair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[str]
    decisive_features: dict[str, list[str]]
    must_exclude: bool = True

    @model_validator(mode="after")
    def _features_cover_candidates(self) -> "ConfusablePair":
        missing = set(self.candidates) - set(self.decisive_features)
        if missing:
            raise ValueError(f"候选 {sorted(missing)} 缺少决定性特征")
        return self
```

校验器保证每个候选都有决定性特征，否则 T2-20 的聚合器会在硬判据分支上遇到 `KeyError`。这份配置一份驱动三处：问诊据此生成关键追问，聚合器据此做排除裁定，判定智能体据此填充 `excluded` 字段。

### 其余子模型

```python
class RedlineSpec(BaseModel):
    """单条流程红线。id 形如 TEA-01，rule 可为逗号分隔的多规则。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    desc: str
    rule: str
    severity: ViolationSeverity
    data: dict = {}


class TimingRule(BaseModel):
    """时机判据，供提示快照生成使用。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    when: dict
    prompt: str


class OutputTemplateSpec(BaseModel):
    """输出模板，模板出事实、模型只改写。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_order: list[str]
    max_chars: int = 400
    required_sections: list[str] = []


class FollowupTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    after_action: str | None = None
    after_days: int
    ask: str
    kind: str = "effect_check"


class FollowupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    triggers: list[FollowupTaskSpec]
    window_based: bool = False


class OfflineQuestion(BaseModel):
    """无传感器时的增强追问。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_key: str
    text: str
    options: list[str] | None = None
    answer_type: AnswerType = AnswerType.SINGLE
```

`max_chars` 默认 400，配合适老化改造：这个上限由后端负责，不留给前端截断，否则适老化验收时会反复返工。`FollowupPolicy.triggers` 里的 `after_days` 是相对天数，药害分支的 T+0 回访不走这里，由 T2-21 的 `branch == "herbicide_damage"` 分支直接构造。

### 便捷读取方法

```python
class ProcessConfig(BaseModel):
    ...
    def required_slot_keys(self) -> list[str]:
        return [s.key for s in self.required_slots if s.required]

    def slot_spec(self, key: str) -> SlotSpec | None:
        return next((s for s in self.required_slots if s.key == key), None)

    def redline_rule_ids(self) -> list[str]:
        """展开逗号分隔的 rule 字段，供闸门反查漏配。"""
        ids: list[str] = []
        for item in self.redlines:
            ids.extend(r.strip() for r in item.rule.split(",") if r.strip())
        return ids

    def check_owns(self, key: str, owner: str) -> bool:
        spec = self.slot_spec(key)
        return spec is not None and spec.owner == owner
```

`redline_rule_ids` 供 T2-12 与闸门做 `checked_rules` 反查，发现"声明了但没执行"的配置错误。`check_owns` 是黑板单一写入者规则的执行点，写入方不符时由 T2-08 的写入函数抛异常。

## 验收标准

- `pytest tests/process_config/test_models.py` 全部通过，至少含：合法 `ProcessConfig` 正例、`extra` 字段存在时报 `ValidationError` 的反例、`ConfusablePair` 缺决定性特征时报 `ValidationError` 的反例
- `python -c "from app.process_config.models import ProcessConfig; print(len(ProcessConfig.model_fields))"` 输出 `16`，字段名与技术方案 9.1 逐一对应
- 覆盖测试断言 `ProcessConfig(process_id="rice", ...).redline_rule_ids()` 对 `rule: "R-INTERVAL,R-MAX-USE"` 返回两元素列表
- 对已构造的配置对象执行属性赋值抛 `ValidationError`（验证 `frozen=True` 生效）
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
