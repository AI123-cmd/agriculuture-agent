# T2-61 现有 ai_router 迁移到流程配置

## 这一项要做什么

把现有 `app/ai_router/` 的三个作物类（`rice.py`、`tobacco.py`、`general.py`）承载的差异拆解并迁移：**按作物写系统提示**并入 `process_config`（角色的 `role_definition` 由 YAML 承载），**httpx 直连模型**并入 `llm`（统一走网关），`app/ai_router/` 目录整体删除。它是"流程配置必须是数据而非代码分支"这条要求的收官动作，也是全局约定里明确的第一批要删除项。

现有关键事实：`ai_router/registry.py` 用装饰器注册三个作物类并暴露 `resolve(crop_name)`；调用方有两处，`app/agent/nodes/diagnose.py` 里 `resolve(crop_tag)` 与 `app/broadcast/skill.py` 里 `resolve("通用农业")`。迁移必须把这两处调用一并改掉，否则删目录会导致导入失败。

## 前置依赖

- 依赖 T2-57：必须已提供任务类型路由与模型档位，取代"按作物选模型"
- 依赖 T2-03：必须已提供 `load_process_config` 与 `get_process_config`，角色的 `role_definition` 从此读取
- 依赖 T2-04：必须已提供七份流程 YAML，其中 `general.yaml` 承接原通用农业兜底的定位
- 依赖 T2-55、T2-56：必须已提供网关统一入口与供应商适配器，取代 `ai_router/base.py` 的 httpx 直连
- 依赖 T2-33：必须已完成问诊接口改造为图驱动，`diagnose.py` 节点被新图取代
- 外部条件：产品说明书附录 A9（烟草流程保留或移除）需产品确认；确认前按占位处理
- 与 T1-12 的关系：本项完成后 `scripts/check_layers.py` 应能通过，`ai_router` 的残留导入会导致失败

## 参考章节

- 技术方案 第2章 2.1 现有技术栈盘点（模型接入现状：按作物三个类，httpx 直连，需改为网关加七条流程配置）
- 技术方案 第2章 2.2 缺口三（没有流程配置层，流程差异被写成代码分支）
- 技术方案 第9章 9.1 流程配置模型（`role_definition` 承载角色定位）
- 技术方案 第10章 10.1 网关职责（统一入口取代直连）
- 技术方案 第4章 4.5 明确不引入的技术（不引入微调等，强调事实来自知识库而非模型权重）
- 全局约定 第二节（第一批要删除 `app/ai_router/`，功能并入 `process_config/` 与 `llm/`）
- 全局约定 第八节（产品说明书附录 A9 待确认项按占位处理）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 删除 | `app/ai_router/base.py` | httpx 直连与 `CropModel` 基类，功能并入 `app/llm/` |
| 删除 | `app/ai_router/registry.py` | 按作物注册与 `resolve`，功能并入 `process_config` 与 `llm/router` |
| 删除 | `app/ai_router/crops/rice.py` | 角色提示词并入 `process_config/seeds/rice.yaml` |
| 删除 | `app/ai_router/crops/tobacco.py` | 见"烟草占位处理"一节 |
| 删除 | `app/ai_router/crops/general.py` | 角色提示词并入 `process_config/seeds/general.yaml` |
| 删除 | `app/ai_router/crops/__init__.py`、`app/ai_router/__init__.py` | 包收尾 |
| 修改 | `app/agent/nodes/diagnose.py` | 移除 `resolve(crop_tag)`，改由新图节点（T2-19）承担 |
| 修改 | `app/broadcast/skill.py` | 移除 `resolve("通用农业")`，改为经网关调用快照文案任务 |
| 修改 | `app/process_config/seeds/rice.yaml` 等 | 补齐 `role_definition`，取自原作物类的 `system_role` |
| 新增 | `tests/process_config/test_roles_migrated.py` | 角色定位已迁移、旧调用点已清除的用例 |
| 修改 | `scripts/check_layers.py` | 确认无对 `app.ai_router` 的残留引用 |

## 实现要点

### 迁移映射：三类差异各自的去处

现有三个作物类承载两类信息：模型标识（`model_id`）与角色提示（`system_role`）。这两类在新结构里的去处完全不同：

| 现有字段 | 现有取值 | 迁移目标 | 说明 |
| --- | --- | --- | --- |
| `model_id` | `deepseek-v4-flash` | `app/llm/router.py` 的档位配置 | 模型选择改由 `task_type` 决定，不再按作物 |
| `system_role` | 各作物专家描述 | `process_config/seeds/{process}.yaml` 的 `role_definition` | 角色定位属流程配置 |
| `name`（注册键） | 水稻 / 烟草 / 通用农业 | `ProcessId` 枚举 | 作物名改为流程标识 |
| `chat`（httpx 直连） | 单端点调用 | `ModelGateway.chat` | 统一入口 |
| `_mock_reply` | 内置兜底文案 | 各智能体的 `degrade` 方法 | 降级出口按智能体定义 |
| `_fallback_reply` | 失败兜底文案 | `app/llm/fallback.py` 的降级出口 | 按 `task_type` 分派 |

`model_id` 并入路由而不是并入配置：方案 4.4 明确模型档位由"环节"决定，不由"作物"决定。分诊用小模型、问诊用主模型，这与问的是水稻还是茶无关。把模型选择留在作物维度会与分层策略冲突。

`system_role` 并入 `role_definition`：`rice.yaml` 的 `role_definition` 现已写"水稻种植专家"一类描述，与原 `RiceModel.system_role` 语义一致，迁移时把原有的具体表述（如"熟悉超级稻栽培、水肥管理、稻飞虱/稻瘟病/纹枯病等病虫害防治"）补进 YAML，避免迁移中丢信息。

### 原代码到新结构的对照

原 `ai_router/base.py` 的直连逻辑在新结构里的分布：

```python
# 原：app/ai_router/base.py（删除）
async def _call_real(self, message, context):
    payload = {"model": model, "messages": [...], "temperature": 0.3}
    resp = await client.post(settings.FARM_LLM_API_URL, json=payload, headers=...)
    content = data.get("answer") or data["choices"][0]["message"]["content"]
```

```python
# 新：调用方经网关（app/llm/ 内实现，业务代码不直接 httpx）
response = await gateway.chat(TaskType.INTERVIEW, messages, InterviewBatch)
```

原逻辑里的三处做法在新结构中被替换：

| 原做法 | 问题 | 新做法 |
| --- | --- | --- |
| `data.get("answer") or ...choices...` 的兼容解析 | 端点差异写进业务代码 | 由适配器（T2-56）各自处理响应形态 |
| `for _ in range(2)` 的裸重试 | 无退避、无分类、不可观测 | 网关候选链加熔断（T2-58） |
| 返回纯文本 `str` | 无结构化输出能力 | 返回 `ModelResponse.parsed` |
| `settings.FARM_LLM_API_URL` 单端点 | 无法按任务选模型 | 路由按档位选供应商与模型 |

`settings.FARM_LLM_API_URL`、`FARM_API_KEY`、`FARM_LLM_TIMEOUT` 保持存在（网关的某个供应商可用它指向该端点），但不再被业务代码直接读取。业务代码不感知端点。

### 烟草占位处理

现有 `ai_router/crops/tobacco.py` 注册了"烟草"作物，但七条流程里没有烟草流程——`ProcessId` 是 rice/tea/rapeseed/chili/weed/pig/general，不含 tobacco。产品说明书附录 A9 的"烟草流程保留或移除"尚未确认。

处理方式按全局约定第八节：**相关任务按占位方式实现，不擅自决定**。具体为：

| 动作 | 做法 |
| --- | --- |
| 删除 `tobacco.py` | 执行，因为它属于要删除的 `ai_router` 目录 |
| 保留角色提示 | 把原 `TobaccoModel.system_role` 的完整文本保存到 `docs/` 下的迁移留存（或 YAML 的注释块），不写入任何 `seeds/*.yaml` |
| 不建 `tobacco.yaml` | 未确认前不新增流程配置，`ProcessId` 不加 `tobacco` |
| 烟草问诊的当前行为 | 分诊无法归到专属流程，回落 `general`，`fallback_reason` 说明"暂未开通该专属流程" |

保留原提示文本是为了确认"保留"时能直接建 `tobacco.yaml`，无需回溯历史代码。把它写成 YAML 注释而非正式配置，是为了让 `load_all_configs` 不把它当作可用流程（`extra="forbid"` 与七流程枚举会拒绝）。

若产品确认"移除"，则删除留存文本；若确认"保留"，按 T2-04 的流程新增动作建 `tobacco.yaml` 并加 `ProcessId.TOBACCO` 成员。这条路径的两端都不需要改本项的实现，只需一次配置新增。

### 调用点的迁移

两处现存调用必须在本项内改完：

```python
# app/agent/nodes/diagnose.py（原）
from app.ai_router.registry import resolve
model = resolve(crop_tag)
reply = await model.chat(message, context)
```

该节点在 T2-33 已被图驱动的新链路取代（`diagnose` 节点变为 T2-19 的判定智能体）。本项确认该文件已删除或不再含 `resolve` 导入。若因过渡保留，则临时改为调用网关：

```python
response = await gateway.chat(TaskType.DIAGNOSE, messages, DiagnosisResult)
```

```python
# app/broadcast/skill.py（原）
from app.ai_router.registry import resolve
model = resolve("通用农业")
```

该文件的兜底逻辑在第三步由快照生成器（T3-07、T3-08）取代。本项把它改为调用 `TaskType.SNAPSHOT_WRITER` 的快照文案任务，或在该步骤完成前先改用零模型模式（直接返回模板原文，见 T2-58）。选零模型模式的理由是：广播文案是改写任务，在快照链路完成前用模板原文最安全，且不依赖任何模型。

`app/api/v1/router.py` 里的 `ai_router` 是 FastAPI 的路由变量名（`from .ai import router as ai_router`），与 `app/ai_router` 包同名但无关。删除包时不要误删这行导入；但为消除歧义，建议把该局部别名改为 `ai_api_router`，避免后续 grep 时混淆。

### 删除后的清理与防回归

删除目录后要跑三项检查：

第一，`grep -rn "app.ai_router" backend/` 与 `grep -rn "from ..ai_router"` 均无结果，确认无残留导入。

第二，`python -c "import app.main"` 能成功导入，确认应用可启动。

第三，`python scripts/check_layers.py` 返回 0，确认分层规则未被破坏。原 `broadcast/skill.py` 导入 `app.ai_router` 这类跨层引用本就不规范（广播层直取模型层），删除后自然消除。

`requirements.txt` 的 `httpx` 保留（网关仍用），但移除只服务于直连的依赖（若 `settings.FARM_LLM_*` 之外还引入了专用客户端则一并清理）。

### 与角色提示词的完整性核对

迁移后要逐流程核对 `role_definition` 已包含原作物类的角色描述要点：

| 流程 | 迁移来源 | 核对要点 |
| --- | --- | --- |
| rice | 原 `RiceModel.system_role` | 含水稻栽培与主要病虫害（稻飞虱、稻瘟病、纹枯病） |
| general | 原 `GeneralModel.system_role` | 含大田作物与经济作物与果蔬的覆盖范围 |
| 其他五流程 | T2-04 新编写 | 无迁移来源，按流程角色定位自拟 |

迁移中不得把 "deepseek-v4-flash" 之类模型名写进 YAML 的 `role_definition`，模型名归路由。核对时若发现 YAML 里出现模型名，视为迁移错误。

## 验收标准

- `python -c "import pathlib; assert not pathlib.Path('app/ai_router').exists()"` 返回 0，目录已删除
- `grep -rn "app\.ai_router\|from \.\.ai_router\|ai_router\.registry" backend/app/` 无结果（`api/v1/router.py` 的局部别名已改名，不再命中）
- `python -c "import app.main"` 返回 0，应用可启动
- `python scripts/check_layers.py` 返回 0，无分层违规
- `pytest tests/process_config/test_roles_migrated.py` 全部通过，至少含五条：`rice.yaml` 的 `role_definition` 含"水稻"，`general.yaml` 的 `role_definition` 含作物覆盖范围描述、任何 `seeds/*.yaml` 的 `role_definition` 不含模型名（如 `deepseek`）、不存在 `tobacco.yaml`、`ProcessId` 不含 `tobacco` 成员
- 断言烟草提示词留存文本存在且被标记为待确认（在留存文件或 YAML 注释中可检索到产品说明书 A9 的引用）
- 断言 `app/llm/` 下无对已删除模块的引用，且 `TaskType.INTERVIEW` 与 `TaskType.DIAGNOSE` 的档位配置存在（原 `model_id` 的替代已就位）
- 断言迁移后问诊走通一遍：以 mock 模式发起一次问诊，`process_id=rice` 时提示词含"水稻"角色定位（角色来自 `rice.yaml` 而非代码）
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
