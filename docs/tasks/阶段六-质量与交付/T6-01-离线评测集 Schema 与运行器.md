# T6-01 离线评测集 Schema 与运行器

## 这一项要做什么

建立三层评估体系的第一层：离线评测集的 Schema 与运行器。多智能体链路变长后错误归因难，因此评测集不是"输入输出对"，而是每道题都标注期望命中的智能体与期望触发的闸门规则，使一次失败能直接指到"哪个环节错了"。运行器用 `pytest` 参数化跑全量集，产出报告写入 `tests/eval/reports/{date}.json`，并与上次报告对比，结论一致率下降即阻断发布。

本项只交付 Schema、运行器与断言骨架，评测集内容由 T6-02 填充。

## 前置依赖

- 依赖 T2-08 已提供：`ConsultationState`、`ConsultStage`、`ProcessId` 枚举（`app/agent/state.py`）。
- 依赖 T2-25 已提供：`build_main_graph()` 与可 `ainvoke` / `astream` 的主图；依赖 T2-55 模型网关在 `USE_MOCK_LLM=true` 时返回预设结构化响应。
- 依赖 T2-03、T2-04：`process_config` 七条流程可加载，评测按 `process_id` 分组需要流程清单。
- 外部条件：`USE_MOCK_LLM=true`；`pytest` + `pytest-asyncio` 可用；知识库版本可固定（评测必须可复现）。
- 现有起点：`backend/tests/` 目前只有 `conftest.py`、`test_ai.py`、`test_auth.py`、`test_farmer.py`，`conftest.py` 用 SQLite 内存库并把 `USE_MOCK_LLM` 设为 `true`；本项在同目录下新增 `eval/` 子包，不修改既有用例。
- `backend/app/core/config.py` 已有 `HARNESS_PASS_RATE_THRESHOLD: float = 0.90` 与 `.env.example` 中的同名项，本项把它接入为评测一致率阈值（见实现要点四）。

## 参考章节

- 技术方案 第14章 14.1 第一层离线评测集（`EvalCase` / `EvalExpectation` 完整定义、分集与规模、执行方式、评测集维护）
- 技术方案 第14章 14.3 第三层线上指标（失败样例回流闭环的入口）
- 技术方案 第16章 16.3 第二步（"补一份评测集"是新增流程的固定动作之一）
- 全局约定 第三节 四条硬性依赖规则、第五节 完成定义、第七节 Mock 约定

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/tests/eval/__init__.py` | 评测子包 |
| 新增 | `backend/tests/eval/schema.py` | `EvalCase` / `EvalInputs` / `EvalExpectation` / `EvalResult` 定义与 `load_cases` |
| 新增 | `backend/tests/eval/runner.py` | `EvalRunner`：固定 temperature、固定知识库版本、跑图、抽取 `EvalResult` |
| 新增 | `backend/tests/eval/test_offline_eval.py` | `pytest` 参数化断言骨架（断言骨架见实现要点三） |
| 新增 | `backend/tests/eval/report.py` | 报告写入 `reports/{date}.json` 与上次报告比对、`compare_consistency` |
| 新增 | `backend/tests/eval/cases/.gitkeep` | 评测集目录占位（内容由 T6-02 填充） |
| 新增 | `backend/tests/eval/reports/.gitkeep` | 报告目录（报告不进版本库，加 `.gitignore`） |
| 修改 | `backend/.gitignore` | 忽略 `tests/eval/reports/*.json`，除 `.gitkeep` |
| 新增 | `backend/tests/eval/test_schema.py` | Schema 自身单测：枚举封闭、必填字段、加载报错 |
| 修改 | `backend/pyproject.toml` | 注册 `eval` marker；评测回归用例打 `@pytest.mark.eval`，与快速单测分开 |

## 实现要点

### 一、Schema 原样保留（技术方案 14.1 原文）

以下两个模型必须逐字对齐技术方案 14.1，不得改名、不得增删字段。字段是评测口径，改名等于换了一套判定标准。

```python
# tests/eval/schema.py
class EvalCase(BaseModel):
    case_id: str
    process_id: ProcessId                      # 属于哪条流程
    scene: str                                 # 场景描述，如"茶园高温干旱"
    inputs: EvalInputs                         # 症状输入、管理记录、环境读数
    expect: EvalExpectation

class EvalExpectation(BaseModel):
    # 1. 受理分流必须正确（对应 2.2 缺口一）
    expect_process: ProcessId
    # 2. 问诊必须问到的槽位（漏问即失败）
    must_ask_slots: list[str] = []
    # 3. 结论允许的取值集合（不要求逐字一致）
    allowed_conclusions: list[str]
    # 4. 期望触发的闸门规则（空集表示不应拦截）
    expect_gate_rules: list[str] = []
    # 5. 禁止出现的表述（安全红线，出现即失败）
    forbidden_phrases: list[str] = []
    # 6. 引用来源必须落在允许集合内
    allowed_sources: list[str] = ["process_config", "knowledge_base", "env_reading"]
```

一处必须显式记录的原文口径差异：14.1 的断言骨架写的是 `case.expect.process`，而 `EvalExpectation` 的字段名是 `expect_process`。本项以字段定义为准，用 `case.expect.expect_process`，并在 `schema.py` 顶部注释注明"方案 14.1 断言骨架的 `case.expect.process` 是笔误"。不新增 `process` 别名属性——别名会让两种写法都能跑，反而掩盖真实字段名。

### 二、EvalInputs 与 EvalResult（本项补充，非方案原文）

`EvalInputs` 承载"症状输入、管理记录、环境读数"三类，与 7.3 主图入参同构；`EvalResult` 是运行器从图终态抽取出的可比对中间量，不是业务对象。

```python
class EvalInputs(BaseModel):
    message: str                                   # 农户首轮表述
    answers: dict[str, str] = {}                   # 预置的各轮追问答案（按 slot_id）
    management: dict[str, str] = {}                # 施肥/用药/灌溉/天气
    env_reading: list[dict] = []                   # 环境读数（无设备则空）

class EvalResult(BaseModel):
    process_id: ProcessId
    asked_slots: list[str]                         # 图过程中问出的所有槽位
    conclusion_key: str                            # DiagnosisResult.conclusion_key
    gate: GateResult                               # 闸门结果（含 rules_hit）
    final_text: str                                # 定稿文本
    sources: list[str]                             # 引用的 SourceType 集合
    rounds: int                                    # 实际轮次（供 14.3 一致口径）
    node_path: list[str]                           # 访问过的节点顺序（供编排回归）
```

`node_path` 是本项的补充字段，不是评测断言的一部分，但把每次评测的节点路径写进报告后，一次"结论不一致"可以立刻区分是"编排走了不同分支"还是"模型输出不同"——这正是 14.1 说的"链路变长后错误归因难"。

### 三、断言骨架（技术方案 14.1 原文，补 allowed_sources 一条）

```python
# tests/eval/test_offline_eval.py
@pytest.mark.eval
@pytest.mark.parametrize("case", load_cases(ProcessId.TEA), ids=lambda c: c.case_id)
@pytest.mark.asyncio
async def test_case(case: EvalCase, eval_runner: EvalRunner):
    result = await eval_runner.run(case)         # 固定 temperature=0，固定知识库版本
    assert result.process_id == case.expect.expect_process
    assert set(case.expect.must_ask_slots) <= set(result.asked_slots)
    assert result.conclusion_key in case.expect.allowed_conclusions
    assert set(case.expect.expect_gate_rules) <= set(result.gate.rules_hit)
    for phrase in case.expect.forbidden_phrases:
        assert phrase not in result.final_text
    assert set(result.sources) <= set(case.expect.allowed_sources)
```

`load_cases` 按流程分集加载，`ids=` 用 `case_id`，使失败报告直接显示"哪条流程的哪道题"。七条流程各写一个 `parametrize`，不合并成一个——14.1 明确"七条流程各自成集"，合并后 `general` 的失败会被其他流程的成功淹没。

`forbidden_phrases` 用 `not in` 而不是分句匹配：安全红线是"出现即失败"，模糊匹配会放过改写后的同类表述，那正是 `R-REDLINE` 要拦的东西。

### 四、运行器与 CI 门禁

```python
class EvalRunner:
    """固定模型温度与知识库版本，保证同一条目可复现。"""
    def __init__(self, graph, knowledge_version: str, temperature: float = 0.0) -> None: ...
    async def run(self, case: EvalCase) -> EvalResult:
        state = build_initial_state(case.inputs)      # 复用 T2-33 的入参构造
        final = await self.graph.ainvoke(state, config={"configurable": {"thread_id": case.case_id}})
        return extract_result(final)
```

一致率口径：`consistency = 通过用例数 / 总用例数`，按流程分别计算并写入报告，另记一个加权总一致率（`general` 权重由 T6-02 定义）。阈值取自 `Settings.HARNESS_PASS_RATE_THRESHOLD`（默认 `0.90`），与 14.1 的"结论一致率下降即阻断发布"合并为两条门禁：

- 绝对门禁：总一致率低于 `HARNESS_PASS_RATE_THRESHOLD`，阻断。
- 相对门禁：总一致率相对上一次报告下降超过 1 个百分点，阻断，即使仍高于绝对阈值。

相对门禁不可省。绝对阈值只防"已经很差"，不防"从 0.98 掉到 0.92"这类回退，而后者正是提示词或流程配置改动引入的典型退化。

### 五、Mock 模式与可复现性

全部评测在 `USE_MOCK_LLM=true` 下跑，`respx` 拦截一切真实 HTTP，CI 不依赖外部模型服务（全局约定第七节）。但这带来一个必须写清的限制：**mock 模式只能回归编排与契约，不能回归模型质量**。模型输出质量的验证通道是 14.5 的人工抽检（T6-09），不是本项。`EvalRunner` 记录 `model_mode` 与 `knowledge_version` 进报告，使"这次评测是哪种模式"永远可查。

## 验收标准

- `pytest backend/tests/eval/test_schema.py` 全部通过，含：`expect_process` 传自由字符串 `"tea "` 抛 `ValidationError`；`allowed_conclusions` 缺省时构造失败（该字段无默认值）；`allowed_sources` 缺省时为 `["process_config", "knowledge_base", "env_reading"]`。
- `load_cases(ProcessId.TEA)` 在 `cases/` 为空时返回空列表且不报错；放入一个非法 JSON 文件时抛带文件名与 `case_id` 的明确错误，不是裸 `JSONDecodeError`。
- `pytest backend/tests/eval/test_offline_eval.py -m eval` 在 T6-02 填充至少一道茶流程题目后全部通过；失败时输出含 `case_id`。
- 故意把某道题的 `allowed_conclusions` 改成不可能命中的值后运行，该用例失败且失败信息显示 `case_id` 与期望集合。
- 故意把某道题的 `forbidden_phrases` 改成 `[""]`（空串会命中任何文本）后运行，该用例失败，证明 `forbidden_phrases` 断言确实被执行而非被跳过。
- 连续跑两次评测，两份 `reports/{date}.json` 中的 `node_path` 完全一致（可复现性）；把第二次报告的一致率人为调低后调用 `compare_consistency`，函数返回"阻断"并给出去向（哪条流程下降）。
- `python -c "from tests.eval.report import compare_consistency"` 可导入；`compare_consistency` 在无历史报告时返回"放行"并提示首次评测。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0（`tests/` 不进 `mypy app` 范围，但 lint 与格式化覆盖）。
