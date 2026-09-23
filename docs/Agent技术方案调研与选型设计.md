# AI Agent 技术方案调研与选型设计

> 面向「话农点金」AI 咨询诊断流程的技术选型报告（2026-08）

## 摘要

本报告调研了 2026 年主流的 AI Agent 技术方案，并围绕「话农点金」的多轮农业诊断流程（接诊 → 症状采集 → 管理追问 → 环境取证 → 综合诊断 → 开处方 → 落实引导 → 回访闭环，含置信度分级与转人工）给出选型结论。

**核心结论：采用「LangGraph 编排 + MCP 工具接入 + 自研业务层 + OpenAI 兼容模型接入」的分层架构。** 即——引入 LangGraph 承担**有状态多轮编排、检查点持久化、人工介入（转专家复核）**这三件最吃框架能力的事；作物路由（`ai_router`）、记忆存取、档案/传感器上下文组装、安全用药规范注入继续**纯自研**；传感器/天气/档案检索等工具用 **MCP 协议**标准化封装。**不采用** CrewAI/AutoGen（状态管理与断点能力不足）、不绑定 OpenAI Agents SDK（锁厂商）、不选低代码平台（Dify/Coze 无法承载深度定制的业务逻辑）。

---

## 一、调研背景与范围

### 1.1 背景

「话农点金」AI 咨询流程已从"一问一答"重构为**农技员式的多轮诊断闭环**，其技术需求具备四个典型特征：

1. **有状态多轮**：诊断分 8 步，需持久化中间状态，支持断点续聊、隔天回访接续。
2. **分支与人工介入**：按置信度分级，低置信度/疑难病情需转人工专家复核后恢复。
3. **工具调用**：诊断需调用传感器数据、天气、农户档案、作物知识库等外部工具。
4. **深度业务定制**：作物垂类路由、三级记忆、安全用药规范、免责声明等强定制逻辑。

这些特征决定了选型重心是"**有状态编排 + 人工介入 + 可观测**"，而非"快速搭一个 demo"。

### 1.2 调研范围与方法

覆盖四类方案：图编排框架、角色型多智能体框架、厂商 SDK、低代码平台，以及工具接入协议 MCP。以 2026 年的版本、Star、生产案例、社区活跃度为判断依据，优先采用有生产落地数据的一手/权威二手来源。

---

## 二、Agent 技术方案全景

### 2.1 分类总览

2026 年真正在用的 Agent 方案可按定位分五类 [2][5][9]：

| 类别 | 代表 | 定位关键词 |
|---|---|---|
| 图编排框架 | LangGraph、Haystack | 有状态工作流、生产级 |
| 角色型多智能体 | CrewAI、AutoGen、Microsoft Agent Framework (MAF) | 多 Agent 协作、原型快 |
| 厂商 SDK | OpenAI Agents SDK、Google ADK、Pydantic AI、Smolagents | 代码优先、绑定生态 |
| 数据/RAG 框架 | LlamaIndex | 检索增强、工具集成强 |
| 低代码平台 | Dify、Coze(扣子)、百炼/千帆 AgentBuilder | 可视化编排、快速上线 |
| 工具接入协议 | MCP | 统一连接外部工具/数据 |

### 2.2 关键框架横向对比

综合多个 2026 横评来源 [1][4][6][7]，主要框架对比如下：

| 框架 | 状态管理/检查点 | 人工介入(HITL) | 多智能体 | 学习成本 | 生产成熟度 |
|---|---|---|---|---|---|
| **LangGraph 1.0** | ✅ 显式 StateGraph + Checkpointer + 时间旅行 | ✅ 原生 `interrupt()` | 需自封装 | 陡 | ✅ 最高（Klarna、Cisco、Towards AI） |
| CrewAI | 依赖外部存储、无原生断点 | 弱 | ✅ 原生角色 | 低 | 中（PoC 快） |
| AutoGen | 消息驱动、状态弱 | 弱 | ✅ 原生对话式 | 中高 | 中（已并入 MAF，维护模式） |
| Microsoft AG (MAF) | ✅ 类型安全状态 + 图级检查点 | 中 | ✅ | 较陡 | 中 |
| Haystack | 中 | 中 | 中 | 陡 | 中 |
| Semantic Kernel | 中 | 中 | 中 | 中 | 中（Azure 生态） |

### 2.3 厂商 SDK 与低代码平台

- **OpenAI Agents SDK**：2026 年 3 月发布后 3 个月下载破百万，增长最快；原生支持 Agent handoff（交接）、Guardrails（护栏）、沙盒，但**强绑定 OpenAI 生态** [11][12]。
- **Google ADK**：代码优先，天然绑定 Vertex/BigQuery [4]。
- **Pydantic AI**：类型化工具 I/O 与校验结构化输出见长 [11]。
- **Dify**：唯一完整 docker-compose 一键自托管的开源平台，5 分钟可起，私有化与数据安全是强项 [9][10]。
- **Coze（扣子）**：SaaS 便捷、绑定字节生态（抖音/飞书）、免费用量，但无法私有化 [10]。

### 2.4 工具接入协议：MCP

MCP（Model Context Protocol）由 Anthropic 于 2024 年 11 月开源，定位为 AI 界的"USB-C"——把"模型 × 工具"的 N×M 连接问题压缩成 N+M：**每个工具只需实现一次 MCP Server，任何兼容 MCP 的模型/框架立即可用**，18 个月内已成为事实标准 [19][20]。底层为 JSON-RPC 2.0（数据层）+ stdio / Streamable HTTP（传输层）[20]。

---

## 三、结合本项目流程的需求拆解

把诊断流程的技术特征，映射到选型要考核的维度：

| 流程环节 | 技术需求 | 对应的选型考察点 |
|---|---|---|
| 接诊 → 取证 → 诊断（多轮） | 有状态、可持久化中间步骤 | 状态管理 + Checkpoint |
| 置信度分级 | 按条件走不同分支 | 条件边 / conditional edge |
| 低置信度转专家 | 暂停 → 人工复核 → 恢复执行 | Human-in-the-loop |
| 回访闭环 | 隔天/隔周接续对话 | Checkpoint 持久化 + 长期记忆 |
| 传感器/天气/档案/知识库 | 调用外部工具 | 工具调用 + MCP |
| 作物垂类路由、安全规范 | 深度业务定制 | 自研业务层，不交框架 |

**结论性判断**：这份需求表里，第 1–5 行恰好是 LangGraph 的原生优势；第 6 行是任何框架都不该代劳的业务逻辑。因此选型收敛到"**LangGraph 做主，其余自研**"。

---

## 四、最终推荐技术方案

### 4.1 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ 模型接入层   直连大模型 API（OpenAI 兼容协议）                  │
│             通义千问 / DeepSeek / 文心 等，httpx 调用即可          │
├─────────────────────────────────────────────────────────────┤
│ 编排层       LangGraph 1.0（StateGraph 状态机）                │
│             · 8 步诊断节点 + 条件边（置信度分支）                │
│             · interrupt() 转人工复核                          │
│             · Checkpointer 持久化（短期记忆 / 断点续聊）          │
│             · 可选 LangSmith 做可观测/审计                      │
├─────────────────────────────────────────────────────────────┤
│ 业务层(自研)  ai_router 作物路由 + memory_service 记忆           │
│             + build_context 档案/传感器上下文 + 安全用药规范        │
├─────────────────────────────────────────────────────────────┤
│ 工具层       MCP Server 封装：传感器查询 / 天气 / 档案检索         │
│             / 作物知识库（模型通过 MCP 统一调用外部工具）           │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 各层选型与理由

**① 编排层 —— LangGraph 1.0（推荐）**

选它的三条硬理由，全部直击本项目需求 [6][7][8][13][14][15]：

- **Checkpoint 持久化**：每步自动捕获完整状态，支持任意节点断点续跑、回放；重复请求可省 40–50% LLM 调用 [6]。
- **原生 human-in-the-loop**：`interrupt()` 让图在指定节点暂停、序列化状态、返回控制权，人工复核后再 `Command(resume=...)` 恢复——精确对应"低置信度转专家复核" [13][14]。这是生产级审计追踪场景（金融、客服）的事实标准能力 [8]。
- **状态显式可控**：用 `TypedDict` + Reducer 显式定义状态，比 CrewAI 的黑盒声明式、AutoGen 的消息驱动更可控，适合需要精准控制诊断步骤的业务。

**② 记忆 —— 复用自研 memory_service，Checkpoint 管短期**

LangGraph 的记忆分两层：短期用 `Checkpoint`（线程内对话状态）、长期用 `Store`（跨线程用户级信息）[16][17][18]。对应本项目已有的三级记忆设计，落地策略是：

- **短期**：交给 LangGraph Checkpoint（当前会话最近 N 轮）；
- **中期/长期**：继续用自研 `memory_service` 写 MySQL（会话摘要 + 用户画像 + 关键事实），**不强依赖框架的 Store**，保持存储层可替换。

**③ 工具层 —— MCP 标准化**

把传感器查询、天气、农户档案检索、作物知识库各自封装成 MCP Server，模型通过 MCP 统一调用 [19][20]。好处是：工具与编排解耦，未来换天气服务商、接新的物联网平台都只动对应 MCP Server，不动诊断图。现有 `app/integrations/` 目录可直接演化为 MCP Server 的实现层。

**④ 模型接入层 —— OpenAI 兼容协议**

不引入 LangChain 的 Model 包装，直接走 OpenAI 兼容协议（通义千问、DeepSeek、文心均已兼容），换厂商不改编排层。

### 4.3 不选其它方案的理由

| 方案 | 不选原因 |
|---|---|
| CrewAI | 状态依赖外部存储、无原生断点，无法支撑"隔天回访接续 + 转人工后恢复" [8] |
| AutoGen | 已并入 MAF、进入维护模式；消息驱动状态弱，不利于精准控制诊断步骤 [2][7] |
| OpenAI Agents SDK / Google ADK | 强绑定单一厂商生态，与"接国内通义/DeepSeek + 私有化部署"诉求冲突 [4][11] |
| LlamaIndex | RAG/数据强，但编排与人工介入能力非核心，可作为知识库检索的补充而非主框架 |
| Dify / Coze 低代码 | 适合快速验证，但无法把作物路由、三级记忆、安全用药规范、免责声明等深度定制逻辑固化进自有后端，且 Dify 需额外运维、Coze 不可私有化 [9][10] |

---

## 五、分阶段落地建议

| 阶段 | 内容 | 目标 |
|---|---|---|
| 阶段一 | LangGraph 搭线性 StateGraph：接诊 → 取证 → 诊断 → 开方，转人工用 `interrupt()` | 跑通主链，验证框架价值 |
| 阶段二 | 接 Checkpointer（如 SQLite/Redis）+ 复用 memory_service，实现回访接续与三级记忆 | 补齐有状态 + 长期记忆 |
| 阶段三 | 把传感器/天气/档案/知识库封装成 MCP Server，接入工具调用 | 打通环境取证闭环 |
| 阶段四 | 接入 LangSmith 做可观测/审计，补全置信度分级与转人工的完整流转 | 生产可运维 |

各阶段均**保持 `ai_router`、安全用药规范、记忆存取自研不变**，模型接入始终走 OpenAI 兼容协议。

---

## 六、结论

在 2026 年的 Agent 技术方案中，**LangGraph 是唯一在「有状态多轮、检查点持久化、原生人工介入」三条线上同时领先、且具备大规模生产验证的框架**，与「话农点金」的多轮诊断、置信度转人工、回访闭环需求高度契合。推荐以 LangGraph 为编排底座、MCP 为工具接入标准，业务层（作物路由、记忆、安全规范）保持自研，模型接入走 OpenAI 兼容协议。这构成一套"框架做编排、业务自研、工具标准化"的可维护、可扩展方案。

---

## 参考来源

1. Scored.tools《Best AI Agent Frameworks 2026》— https://scored.tools/downloads/best-ai-agent-frameworks-2026.pdf
2. CSDN《2026 年 AI Agent 框架横评：10 大框架优缺点对比 + 选型指南》— https://blog.csdn.net/m0_72796578/article/details/163945081
3. Deploybase《Best AI Agent Frameworks in 2026》— https://deploybase.ai/articles/best-ai-agent-frameworks
4. Inductivee《Agentic AI Frameworks in 2026》— https://inductivee.com/blog/agentic-ai-frameworks-comparison
5. AliceLabs《AI Agent Frameworks 2026: Production-Tested Ranking》— https://alicelabs.ai/en/insights/best-ai-agent-frameworks-2026
6. 什么值得买《LangGraph/AutoGen/CrewAI 哪个才是 2026 年最优选》— https://post.m.smzdm.com/p/a4qrez48/
7. CSDN 智能体开发者社区《四强争霸：LangGraph、CrewAI、AutoGen 与微软 MAF》— https://adg.csdn.net/6a66f67d662f9a54cb949dec.html
8. 什么值得买《LangGraph、CrewAI、AutoGen 怎么选？一张图看懂》— https://post.m.smzdm.com/p/avgqp6m9/
9. MCP 技术社区《2026 年 AI Agent 技术全景：12 大主流框架深度对比》— https://mcp.csdn.net/6a39ddc0662f9a54cb82df4b.html
10. CSDN《Dify 简要说明（Dify vs 扣子）》— https://blog.csdn.net/qq_16430177/article/details/158125845
11. GitHub awesome-ai-agents-2026《FRAMEWORKS.md》— https://github.com/Supersynergy/awesome-ai-agents-2026/blob/main/FRAMEWORKS.md
12. AI工具箱《2026 年 AI Agent 编排工具终极指南》— https://www.aitoollab.cn/articles/multi-agent-ai-tools-guide-2026/
13. CSDN《LangGraph 基本使用：中断与恢复机制》— https://blog.csdn.net/m0_51574586/article/details/161891327
14. Rentier Digital《Human-in-the-loop with LangGraph》— https://rentierdigital.xyz/blog/human-in-the-loop-langgraph
15. arXiv《Graph-Based Agentic AI with LangGraph》— https://arxiv.org/pdf/2607.19297
16. LangGraph 中文站《持久化》— https://langgraph.com.cn/concepts/persistence.1.html
17. CSDN《LangGraph 中的短期记忆与长期记忆：从 Checkpoint 到 Store》— https://blog.csdn.net/qq_52001969/article/details/163395944
18. 51CTO《一文吃透 LangGraph 的"长短期记忆"架构》— https://blog.51cto.com/u_17561223/14873808
19. CSDN《MCP 协议到底是什么？2026 年 AI Agent 最热门的工具接入标准详解》— https://blog.csdn.net/aitool/article/details/163384706
20. 掘金《2026 还不懂 MCP 就真的晚了》— https://aicoding.juejin.cn/post/7676375370355736603