# AI 智能体技术实现详细方案

> 基于「话农点金」现有后端架构，将 AI 咨询从"一问一答 + mock"改造为 LangGraph 驱动的「多轮诊断闭环」。本版**不含转人工、前端零改动**，给出精确目录结构、文件修改/新增清单与分阶段实施计划。

---

## 一、目标与范围

### 1.1 现状

| 现状 | 说明 |
|---|---|
| 编排 | `ai_service.chat()` 一次调用：`build_context → memory.recall → resolve(cropTag) → model.chat → 落库`，无多轮、无分支 |
| 模型接入 | `ai_router/base.py` 的 `_call_real` 用 `self.name`（"水稻"）当 model id，真实接入会失败 |
| 记忆 | `memory_service` 的 `recall` 返回空摘要 + 最近 5 条事实；`remember` 只是截断字符串 |
| 工具 | 传感器/天气/档案没有统一工具接口，`build_context` 只读档案和画像 |

### 1.2 改造目标

实现「接诊 → 症状采集 → 管理追问 → 环境取证 → 综合诊断 → 开处方」的有状态多轮流程：

- 置信度分级：高 → 直接开方；中/低 → **引导补充关键证据**（多轮追问，设上限防死循环）；
- 工具化取证：传感器、天气、档案、知识库通过 **MCP** 标准化接入；
- 三级记忆：短期（Checkpoint）+ 中期/长期（自研 memory_service）；
- 作物垂类路由继续自研，模型接入走 OpenAI 兼容协议并修正 model id；
- **对外契约保持 `{sessionId, reply, relatedSuggestions}` 纯文本，前端零改动**。

> 转人工（human-in-the-loop）本期不做，但编排层预留了扩展点，后续可在一个节点加 `interrupt()` 平滑引入。

---

## 二、总体架构（分层）

```
┌─ 模型接入层 ─ 直连 OpenAI 兼容大模型 API（httpx，修 model_id）─────────┐
│              app/ai_router/（base.py + crops/*，自研，改造）            │
├─ 编排层 ──── LangGraph StateGraph（新增 app/agent/）──────────────────┤
│              state.py / graph.py / nodes/* / checkpointer.py / confidence.py │
├─ 业务层 ──── 自研（改造）──────────────────────────────────────────────┤
│              memory_service（记忆） / profile_service（画像）           │
├─ 工具层 ──── MCP Server（新增 app/tools/）─────────────────────────────┤
│              sensors / weather / profile / knowledge                   │
└────────────────────────────────────────────────────────────────────────┘
```

**职责边界**：编排层管"流程怎么走"，业务层管"数据怎么存"，工具层管"外部数据怎么取"，三者通过 LangGraph 的 State 和工具调用解耦。

---

## 三、依赖变更

### 3.1 `backend/pyproject.toml` 与 `backend/requirements.txt`（修改）

新增（同步两份文件）：

```
langgraph>=0.2.0
langgraph-checkpoint-sqlite>=2.0.0      # 短期记忆/断点持久化（生产可换 postgres/redis saver）
mcp>=1.0.0                               # 工具层 MCP Server
langgraph-mcp-adapters>=0.1.0            # 把 MCP tools 接入 LangGraph 作为 tool node
```

> `httpx` 版本已锁 `<0.28`（与 TestClient 兼容），**不要**为新增依赖改动该约束。

---

## 四、目录结构总览

### 4.1 新增目录与文件（后端，共 18 个）

```
backend/app/
├─ agent/                              # ★新增：LangGraph 编排层
│  ├─ __init__.py
│  ├─ state.py                         # AgentState TypedDict + 步骤枚举 + Reducer
│  ├─ graph.py                         # build_diagnosis_graph()：组装节点/边/条件路由
│  ├─ checkpointer.py                  # get_checkpointer()：SQLite 单例
│  ├─ confidence.py                    # 置信度分级（规则化、可测试）
│  └─ nodes/                           # 各诊断节点
│     ├─ __init__.py
│     ├─ intake.py                     # 接诊/建档确认
│     ├─ collect_symptoms.py           # 症状采集
│     ├─ collect_management.py         # 管理追问（六问）
│     ├─ fetch_environment.py          # 环境取证（调用工具）
│     ├─ diagnose.py                   # 综合诊断（调 ai_router + 置信度）
│     └─ prescribe.py                  # 开处方（结构化 + 安全规范，转文本）
├─ tools/                              # ★新增：工具层 MCP Server
│  ├─ __init__.py
│  ├─ sensors.py                       # 传感器查询工具
│  ├─ weather.py                       # 天气工具
│  ├─ profile.py                       # 农户档案检索工具
│  ├─ knowledge.py                     # 作物知识库检索工具
│  └─ catalog.py                       # build_tools()：汇总 MCP tools → LangGraph tool list
└─ ...
```

### 4.2 现有目录保持不动

`ai_router/`、`services/`、`integrations/`、`models/`、`schemas/`、`api/`、`core/`、`db/` 保留，仅按第五章清单局部改造。

---

## 五、文件变更总览

### 5.1 新增文件（后端，18 个）

| 文件 | 职责 |
|---|---|
| `app/agent/__init__.py` | 暴露 `build_graph` / `get_agent` |
| `app/agent/state.py` | `AgentState`（TypedDict）、`Step` 枚举、`round` 计数 |
| `app/agent/graph.py` | 组装诊断状态机（节点 + 条件边 + 回环） |
| `app/agent/checkpointer.py` | Checkpointer 单例（SQLite） |
| `app/agent/confidence.py` | 证据信号 → 置信度分级 |
| `app/agent/nodes/__init__.py` | 节点导出 |
| `app/agent/nodes/intake.py` | 接诊节点 |
| `app/agent/nodes/collect_symptoms.py` | 症状采集节点 |
| `app/agent/nodes/collect_management.py` | 管理追问节点 |
| `app/agent/nodes/fetch_environment.py` | 环境取证节点 |
| `app/agent/nodes/diagnose.py` | 诊断节点 |
| `app/agent/nodes/prescribe.py` | 开处方节点（结构化 → 纯文本） |
| `app/tools/__init__.py` | 工具包入口 |
| `app/tools/sensors.py` | MCP 传感器工具 |
| `app/tools/weather.py` | MCP 天气工具 |
| `app/tools/profile.py` | MCP 档案工具 |
| `app/tools/knowledge.py` | MCP 知识库工具 |
| `app/tools/catalog.py` | 工具目录/注册 |

### 5.2 修改文件（后端，13 个）

| 文件 | 修改点 |
|---|---|
| `pyproject.toml` / `requirements.txt` | 新增 langgraph / mcp 等依赖（§3） |
| `app/core/config.py` | 新增 `LLM_DEFAULT_MODEL`、`CHECKPOINT_DB_URL`、`CONFIDENCE_*` 阈值、`MAX_COLLECT_ROUNDS`、`MCP_*` 开关；保留 `USE_MOCK_LLM` |
| `app/main.py` | `lifespan` 初始化 checkpointer 与 agent graph（单例） |
| `app/api/v1/ai.py` | `POST /chat` 改为调用 agent，**接口路径与响应结构不变** |
| `app/schemas/ai.py` | `ChatRequest` 保持不变（`message/sessionId/cropTag/imageUrl`），**不新增必填字段** |
| `app/services/ai_service.py` | `chat()` 改为"喂入 LangGraph"并把最终 `reply` 落库返回；`history()` 不变 |
| `app/services/memory_service.py` | `recall` 返回真实摘要与事实；`remember` 改为摘要 + 事实抽取（规则或轻量 LLM） |
| `app/ai_router/base.py` | 增加 `model_id`；`_call_real` 用 `self.model_id`；补超时、重试、空响应兜底；`chat()` 支持多段信息入参 |
| `app/ai_router/crops/general.py` | 设 `model_id` + 调整诊断式 system prompt |
| `app/ai_router/crops/rice.py` | 同上 |
| `app/ai_router/crops/tobacco.py` | 同上 |

### 5.3 前端改动

**零改动（目标）**：后端继续以 `{sessionId, reply, relatedSuggestions}` 返回，`reply` 是已经拼接好的纯文本（追问或处方），`relatedSuggestions` 作为追问时的快捷选项。现有 `chat.ts` 已渲染 `reply` 与 `relatedSuggestions`，因此：

| 文件 | 是否需改 |
|---|---|
| `pages/ai/chat.ts` / `chat.wxml` | 否（除非想要更漂亮的处方排版，属可选项） |
| `api/ai.ts` | 否（返回结构不变） |
| `types/api.d.ts` | 可选：仅当想给 `relatedSuggestions` 加注释时对齐 |

---

## 六、核心模块详细设计

### 6.1 状态定义（`agent/state.py`）

```python
from typing import TypedDict, Optional, Annotated, List
from langgraph.graph import add_messages

class Step(str, Enum):
    INTAKE = "intake"
    COLLECT_SYMPTOMS = "collect_symptoms"
    COLLECT_MANAGEMENT = "collect_management"
    FETCH_ENVIRONMENT = "fetch_environment"
    DIAGNOSE = "diagnose"
    PRESCRIBE = "prescribe"

class AgentState(TypedDict, total=False):
    session_id: str
    user_id: str
    crop_tag: str
    step: Step                              # 当前环节（checkpoint 持久化）
    round: int                              # 取证轮数（防死循环）
    image_url: Optional[str]
    symptoms: str
    management: dict                        # 六问答案
    environment: dict                       # 传感器/天气快照
    diagnosis: Optional[dict]
    confidence: float
    confidence_level: str                   # high / medium / low
    messages: Annotated[List, add_messages] # 会话消息
```

`step` 即"农技员问到第几步"的持久化表示，Checkpointer 按 `thread_id = session_id` 保存，实现断点续聊。

### 6.2 诊断图（`agent/graph.py`）

```
START
  │
  ▼
[intake 接诊]────────(档案/作物未定)──► 返回"请确认作物与生长阶段"
  │ 已有档案
  ▼
[collect_symptoms 症状采集]──(症状不足)──► 返回追问(图片+症状描述)
  │
  ▼
[collect_management 管理追问]──(六问不全)──► 返回追问(施肥/打药/浇水/天气)
  │
  ▼
[fetch_environment 环境取证] ── 调 MCP 工具：传感器/天气/档案/知识库
  │
  ▼
[diagnose 综合诊断] ── resolve(crop_tag) → model.chat，产出 diagnosis + confidence
  │
  ├─(confidence in low/medium 且 round < MAX)──► 回 collect_symptoms 补证据（round+1）
  │
  └─(high，或已达 MAX 轮)──► [prescribe 开处方] ── 结构化处方 + 安全规范
                                │
                                ▼
                              END（返回诊断/处方文本给用户）
```

**设计要点**：取证节点返回的是**追问**而非最终答案——这是"多轮"的核心。`round` 计数避免"证据一直不全"导致死循环；达到上限仍证据不足时，走 `prescribe` 输出**保守建议 + 免责提示**。

### 6.3 置信度分级（`agent/confidence.py`）

规则化、确定性、可单测：

```python
def assess(state: AgentState) -> tuple[float, str]:
    signals = {
        "has_image": bool(state.get("image_url")),
        "symptoms_complete": bool(state.get("symptoms")),
        "management_complete": all(k in state.get("management", {})
                                   for k in ("fertilize", "pesticide", "water", "weather")),
        "environment_consistent": not state.get("evidence_conflict", False),
    }
    score = sum(signals.values())
    level = "high" if score >= 3 and signals["has_image"] else \
            "medium" if score >= 2 else "low"
    return score, level
```

| level | 处理 |
|---|---|
| high | 进 `prescribe` |
| medium / low | 回取证节点补一轮；达 `MAX_COLLECT_ROUNDS` 后转 `prescribe`（保守方案 + 免责） |

### 6.4 开处方（`agent/nodes/prescribe.py`）

`prescribe` 产出**结构化处方**（`summary / measures[] / pesticide[] / safety_notes[]`），随后**序列化为纯文本**拼进 `reply`，例如：

```
【初步判断】叶片黄化，结合土壤偏酸，疑似养分吸收障碍。
【农事措施】1. 增施有机肥改良土壤；2. 控制浇水避免积水。
【用药建议】如需防治，请选用……（本条建议仅供参考，请结合当地植保指导）。
【提醒】当前湿度较高，注意田间通风排湿。
```

安全用药规范（轮换用药、单季≤2 次等）由 `prescribe` 强制注入。结构化的 `prescription` 数据同时以 JSON 存入消息的 `meta`（见 §八，可选），供后续历史富展示用，但**本轮对外只返回文本**。

### 6.5 工具层（`app/tools/`，MCP 标准化）

每个工具用 `mcp` SDK 定义成独立 MCP Server，由 `catalog.build_tools()` 汇总、经 `langgraph-mcp-adapters` 转成 LangGraph tool：

| 工具 | MCP 工具名 | 作用 | 数据来源 |
|---|---|---|---|
| 传感器 | `query_sensors` | 读地块空气/土壤温湿度、光照、pH/EC | `Device` + 传感器平台 |
| 天气 | `query_weather` | 当前/预报天气 | `app/integrations/weather.py` |
| 档案 | `query_profile` | 拉取档案 + 作物 + 画像标签 | `FarmerProfile`/`Crop`/`UserProfileTag` |
| 知识库 | `search_knowledge` | 检索病虫害/施肥知识 | 作物知识库 |

换天气服务商、接新物联网平台只改对应 MCP Server，不动诊断图；`app/integrations/` 是底层实现，`tools/*.py` 是其 MCP 包装。

### 6.6 记忆整合

| 层级 | 实现 | 变更 |
|---|---|---|
| 短期 | LangGraph Checkpointer（thread=session_id） | 新增 checkpointer 配置 |
| 中期（摘要） | `memory_service` 真摘要 | 修改 `remember` |
| 长期（事实/画像） | `memory_service` 写 `MemorySummary` + `UserProfileTag` | 修改 `remember` 事实抽取 |

`diagnose` 节点调 `memory_service.recall()` 注入中期/长期记忆，短期历史由 Checkpointer 提供。

### 6.7 模型接入改造（`ai_router/`）

1. **修 model id**：`base.py` 增加 `model_id`，`_call_real` 的 `payload["model"]` 改用 `self.model_id or settings.LLM_DEFAULT_MODEL`。
2. **支持多段入参**：`chat(message, context)` 的 `context` 承载"取证信息 + 记忆"，诊断 prompt 据此生成结构化 JSON。
3. **容错**：超时重试、空响应兜底到 `_mock_reply` 风格的安全提示。

每个 `crops/*.py` 只需声明 `model_id` 与 system prompt，路由逻辑不动。

---

## 七、API 契约（保持向后兼容）

请求与响应**均不变**：

```python
class ChatRequest(BaseModel):
    message: str
    sessionId: str
    cropTag: str | None = None
    imageUrl: str | None = None

# 响应保持:
# { sessionId, reply: str, relatedSuggestions: list[str], tagsAdded: list[str] }
```

- `reply`：追问或处方的**纯文本**，前端直接当 AI 消息渲染；
- `relatedSuggestions`：追问时的快捷选项（对应结构化问题的 options 平铺成文本），处方时为后续追问建议；
- 多轮靠前端**沿用同一 `sessionId` 连续发送**实现（现有多轮聊天已支持）。

因此前端 `api/ai.ts`、`chat.ts`、`chat.wxml` **无需改动**。

---

## 八、数据库变更（可选，本期可暂缓）

- 为将来"历史页富展示处方/追问"，可在 `AIMessage` 增 `meta`(JSON) 字段存结构化数据；**本期对外是纯文本，此字段非必需**，可在阶段四按需加 Alembic 迁移。

---

## 九、可维护性与扩展性设计

| 扩展诉求 | 做法 | 涉及文件 |
|---|---|---|
| 新增作物专家模型 | `ai_router/crops/` 加 `@register("作物名")` 文件 + `model_id` | 新增 1 文件 |
| 新增诊断环节 | `agent/nodes/` 加节点，`graph.py` 插边 | 新增 1 文件 + 改 1 处 |
| 新增工具（虫情灯等） | `tools/` 加 MCP tool，`catalog.py` 注册 | 新增 1 文件 + 改 1 处 |
| 换模型厂商 | 改 `LLM_DEFAULT_MODEL` / 各 crop `model_id` | 改配置 |
| 未来加"转人工" | 在 `prescribe` 前插一个 `interrupt()` 节点 | 加 1 节点 + 改边 |

**单向依赖**：`api → services → agent(编排) → tools/ai_router`，禁止反向引用。

---

## 十、分阶段实施计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| 阶段一（主链） | 落地 `agent/state|graph|nodes|checkpointer`；`ai.py` 接入 graph；`base.py` 修 model_id；`USE_MOCK_LLM=True` 跑通 | 同一会话能按步骤追问→诊断→开方（纯文本），断点续聊可用 |
| 阶段二（记忆） | 完善 `memory_service` 真摘要 + 事实抽取；诊断节点注入记忆 | 跨会话能引用历史事实 |
| 阶段三（工具） | 落地 `tools/` 四个 MCP Server + `catalog` + adapters | 环境取证能拉传感器/天气快照 |
| 阶段四（可选颗粒） | `AIMessage.meta` 结构化存储；前端处方/追问富展示（可选） | 历史页能还原结构化内容 |

---

## 十一、风险与对策

| 风险 | 对策 |
|---|---|
| LangGraph 学习曲线陡 | 阶段一只搭"线性 + 一条回环边"，跑通后再扩展 |
| 真实 LLM 长耗时 | 保留 `httpx` 超时 + 前端 loading；后续可升级流式 |
| 取证不足/死循环 | `round` 计数 + `MAX_COLLECT_ROUNDS`，超限输出保守建议 |
| 处方合规风险 | `prescribe` 强制注入安全用药规范 + 免责声明 |
| checkpointer 数据膨胀 | 生产换 Postgres/Redis saver，设置保留策略 |

---

## 附录：文件清单速查

- **新增后端 18 个**：`agent/`(12) + `tools/`(6)
- **修改后端 13 个**：`pyproject.toml`、`requirements.txt`、`core/config.py`、`main.py`、`api/v1/ai.py`、`schemas/ai.py`、`services/ai_service.py`、`services/memory_service.py`、`ai_router/base.py`、`ai_router/crops/{general,rice,tobacco}.py`
- **前端改动：0（目标零改动）**，`pages/ai/chat.*`、`api/ai.ts` 均不动