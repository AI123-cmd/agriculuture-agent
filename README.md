# 话农点金（HNDJ）

面向农户与农技人员的微信小程序，提供**农事播报、环境监测、AI 农技诊断问答**等能力。小程序端采用原生微信小程序 + TypeScript，后端采用 FastAPI（全异步），AI 诊断基于 **LangGraph 多智能体编排**，配套**流程配置层、安全闸门、知识库检索与模型网关**，对接 DeepSeek 兼容大模型。

![系统架构](docs/photo/architecture.svg)

## 技术栈

| 层 | 技术 |
| --- | --- |
| 小程序端 | 原生小程序 · TypeScript · glass-easel · 自定义 tabBar · 适老化样式 token |
| 后端 | Python 3.11+ · FastAPI · SQLAlchemy 2.0（async）· Alembic · ARQ（异步任务） |
| 数据 | PostgreSQL 16+（pgvector 向量检索 · TimescaleDB 时序）· Redis 7 |
| AI | LangGraph 多智能体 · MCP 工具层 · PostgreSQL 检查点 · DeepSeek 兼容模型 |

> 底座已统一为 **PostgreSQL 单一栈**：知识检索的「向量相似度 + 元数据过滤」在一条 SQL 内完成，LangGraph 检查点由 `AsyncPostgresSaver` 持久化，取代了早期的 MySQL + SQLite 组合。

## 界面预览

<table>
  <tr>
    <td align="center" width="25%"><img src="docs/photo/screen-home.png" width="200" alt="首页"><br><sub>首页 · 农事播报与环境摘要</sub></td>
    <td align="center" width="25%"><img src="docs/photo/screen-ai-chat.png" width="200" alt="AI 咨询"><br><sub>AI 咨询 · 多轮农技诊断</sub></td>
    <td align="center" width="25%"><img src="docs/photo/screen-history.png" width="200" alt="历史对话"><br><sub>历史对话 · 会话列表</sub></td>
    <td align="center" width="25%"><img src="docs/photo/screen-mine.png" width="200" alt="我的"><br><sub>我的 · 档案与设置</sub></td>
  </tr>
</table>

## 目录结构

```
agriculuture-agent/
├── miniprogram/            # 微信小程序前端
│   ├── pages/              # 页面（login/profile/index/ai/history/mine/env）
│   ├── api/                # 后端接口封装（ai/auth/farmer/broadcast/environment/...）
│   ├── components/         # 自定义组件（sprout-mascot）
│   ├── custom-tab-bar/     # 自定义 tabBar
│   ├── styles/             # 公共样式 token（tokens.wxss）
│   ├── utils/              # 请求、鉴权 guard、常量、定位、语音（voice）
│   └── app.json            # 页面/权限/tabBar 配置
├── backend/                # FastAPI 后端
│   ├── app/
│   │   ├── agent/          # LangGraph 编排（state/agents/contracts/graph/prompts/blackboard）
│   │   ├── broadcast/      # 农事播报：输入采集/规则/文案/快照/触达
│   │   ├── gate/           # 安全闸门规则引擎（白名单/剂量/间隔期/红线等）
│   │   ├── knowledge/      # 知识库（索引/混合检索/地区回退/重排/种子 JSONL）
│   │   ├── llm/            # 模型网关（供应商适配/路由/档位/熔断降级/计量）
│   │   ├── process_config/ # 流程配置（七条流程 YAML + 红线 + 版本化）
│   │   ├── tools/          # MCP 工具层（传感器/天气/档案/知识库/目录）
│   │   ├── services/       # 业务服务层
│   │   ├── repositories/   # 数据访问层（归属安全基类 OwnedRepository）
│   │   ├── api/            # 路由 + 鉴权依赖
│   │   ├── models/         # SQLAlchemy 模型
│   │   ├── schemas/        # 接口出入参契约
│   │   ├── integrations/   # 外部集成（微信/天气/逆地理/存储）
│   │   ├── observability/  # 日志与指标
│   │   ├── main.py         # 应用入口
│   │   └── worker.py       # ARQ 任务进程（发件箱投递/触达扫描/回访）
│   ├── alembic/            # 数据库迁移
│   ├── scripts/            # 迁移、分层检查、CI 脚本
│   ├── tests/              # pytest 测试
│   ├── requirements.txt
│   └── pyproject.toml
├── docs/                   # 设计与任务文档
│   ├── tasks/              # 分阶段改造任务总表（见 docs/tasks/README.md）
│   └── photo/              # 架构与界面资源
├── scripts/                # 前端样式 token 门禁脚本
└── project.config.json     # 小程序项目配置（miniprogramRoot=miniprogram/）
```

## 快速开始

### 1. 后端

前置：PostgreSQL 16+（需带 `pgvector` 与 `timescaledb` 扩展，推荐 `timescale/timescaledb-ha:pg16` 镜像）、Redis 7+。

```bash
cd backend

# 创建并激活虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（复制样例并填写）
cp .env.example .env   # Windows: copy .env.example .env

# 初始化数据库（PostgreSQL 需先建库 huanong）
alembic upgrade head

# 启动 API 服务
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后（`DEBUG=true` 时开放文档）：

- 接口文档（Swagger UI）：<http://127.0.0.1:8000/docs>
- ReDoc：<http://127.0.0.1:8000/redoc>

### 2. 后台任务进程（可选）

发件箱投递、触达扫描与回访登记由独立的 ARQ worker 承担，与 API 进程分开部署：

```bash
cd backend
arq app.worker.WorkerSettings
```

### 3. 小程序端

1. 用微信开发者工具导入项目根目录（`project.config.json` 已配置 `miniprogramRoot` 为 `miniprogram/`）。
2. 后端地址在 [miniprogram/utils/config.ts](miniprogram/utils/config.ts) 中配置（默认本机 `http://127.0.0.1:8000`）。
3. 本地联调时在开发者工具「详情 → 本地设置」勾选「不校验合法域名」。

## 环境变量

核心配置见 [backend/.env.example](backend/.env.example)，常用项：

| 变量 | 说明 |
| --- | --- |
| `ENV` | 运行环境，取值 `development` / `staging` / `production`（启动时校验） |
| `DATABASE_URL` | 业务库异步连接串，driver 必须是 `asyncpg`（`postgresql+asyncpg://...`） |
| `CHECKPOINT_DB_URL` | LangGraph 检查点连接串，driver 为裸 `postgresql`（psycopg v3） |
| `CHECKPOINT_SCHEMA` | 检查点表所在 schema（默认 `checkpoint`） |
| `REDIS_URL` | Redis 地址（缓存、额度、事件发件箱队列） |
| `REDIS_REQUIRED` | 生产环境置 `true`，缺失时启动自检拒绝启动 |
| `JWT_SECRET` / `JWT_ALGORITHM` | 令牌签名密钥与算法（密钥只放 `.env`，勿提交） |
| `GATE_MODE` | 安全闸门模式：灰度期 `shadow`，全量生产 `enforce` |
| `CORS_ORIGINS` | CORS 白名单（JSON 数组或逗号分隔），生产为空或含 `*` 时拒绝启动 |
| `EMBEDDING_DIM` | 知识条目向量维度，须与 embedding 模型输出一致 |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 小程序凭证（secret 缺失时登录走 mock） |
| `FARM_LLM_API_URL` / `FARM_API_KEY` | 农业大模型兼容 API 地址与密钥 |
| `USE_MOCK_LLM` | `true` 时模型调用走确定性 mock（联调与测试用） |
| `ZERO_MODEL_MODE` | `true` 时关闭全部模型调用，仅用规则层能力产出文案 |
| `KNOWLEDGE_REVIEW_OPERATOR_IDS` | 知识条目核准接口的运营白名单（空 = 无人可调用） |

## API 接口

统一响应结构：`{ "code": 200, "message": "success", "data": ... }`，除登录外均需 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | 微信登录 |
| POST | `/api/logout` | 登出（令牌吊销） |
| GET/POST | `/api/farmer/profile` | 获取 / 保存农户档案 |
| GET | `/api/location/reverse-geocode` | 逆地理编码 |
| POST | `/api/location/set` | 设置农户位置 |
| GET/POST | `/api/broadcast/snapshot` | 读取 / 生成当日提示快照 |
| POST | `/api/broadcast/items/{item_id}/execute` | 提示条目执行回执（勾选/取消） |
| GET | `/api/environment/summary` | 环境监测汇总 |
| GET | `/api/environment/detail` | 环境监测详情 |
| POST | `/api/ai/chat` | AI 农技诊断对话（图驱动） |
| GET | `/api/ai/history` | 对话历史 |
| DELETE | `/api/ai/history/{session_id}` | 删除对话 |
| POST | `/api/ai/prescription/{card_id}/ledger` | 处方卡存入农事台账（幂等） |
| GET | `/api/subscription/quota` | 查询推送额度 |
| POST | `/api/subscription/grant` | 记录订阅授权结果 |
| POST | `/api/knowledge/review/{knowledge_id}` | 知识条目核准（运营白名单） |
| POST | `/api/user/avatar/upload` | 上传头像 |
| DELETE | `/api/user/cancel` | 注销账户 |

## AI 农技诊断

诊断链路为「受理即分流 → 看现场 → 问五情 → 查依据 → 下判断 → 开方 → 安全校验 → 登记回访」的闭环，基于 LangGraph 状态机实现：

![AI 诊断状态机](docs/photo/ai-diagnosis-flow.svg)

- **受理即分流**：进入图后的第一条边即为条件边，按 `processHint` 或分诊结果路由到七条流程子图之一（水稻 / 茶 / 油菜 / 辣椒 / 杂草 / 养猪 / 通用兜底），不存在「先问症状再猜流程」。
- **七条流程子图**：由 `app/agent/graph/factories.py` 工厂函数装配，配置来自 `app/process_config/seeds/*.yaml`。
- **多智能体分工**：`triage`（分诊）、`field_observer`（现场采集）、`interviewer`（问诊）、`retriever`（证据核查）、`diagnoser`（判定）、`panel`（受限并行会诊）、`prescriber`（处方）、`report_guard`（报告守卫）、`handoff`（转人工）等。
- **安全闸门**：处方产出后经独立闸门子图判定（`allow` / `degrade` / `block`），命中红线一律转人工，所有规则可单测（见 `app/gate/rules/`）。
- **知识库取证**：`app/knowledge/` 提供向量 + 关键词混合检索、地区回退匹配与重排序，药剂白名单、安全间隔期等规则的比对基准均来自知识库。
- **模型网关**：`app/llm/` 统一供应商适配、任务路由与档位、熔断降级与 token 计量；`ZERO_MODEL_MODE` 下可完全断开模型仅用规则。
- **记忆与断点**：短期由 PostgreSQL 检查点器持久化，支持中断恢复与断点续问；长期用 `memory_service` 存取画像与关键事实。

## 农事播报

播报生成采用**规则优先**策略，规则未命中时逐级兜底，保证任何环境下都能产出当日提醒：

![农事播报生成链路](docs/photo/broadcast-flow.svg)

- **输入采集**：`app/broadcast/inputs/` 收敛环境事实（三级回退）、农事历、问诊与回访信号。
- **规则引擎**：`app/broadcast/rules/` 提供阈值规则（高温/低温/墒情/高湿/降雨/大风）与病虫害气象风险。
- **优先级与条数**：`app/broadcast/priority.py` 排序并控制当日条数。
- **文案生成**：`app/broadcast/writer.py` 由模板出事实，必要时经模型网关润色。
- **快照与触达**：`app/broadcast/snapshot.py` 幂等生成快照，`notifier.py` + `window.py` 在最佳窗口内按额度推送。

## 质量门禁与测试

后端本地与 CI 共用一份门禁序列：

```bash
cd backend
bash scripts/ci.sh        # ruff format/check → mypy → check_layers → pytest
```

或单独运行测试：

```bash
cd backend
.venv\Scripts\python.exe -m pytest -q     # Windows
python -m pytest -q                      # macOS / Linux
```

- 测试使用 PostgreSQL 测试库（`TEST_DATABASE_URL`）与 mock LLM 隔离环境，不调用真实大模型。
- `scripts/check_layers.py` 强制分层依赖（如闸门不得依赖智能体层、禁止直接 `db.query`）。
- 前端适老化样式由根目录 `scripts/check_wxss_tokens.mjs` 门禁校验（字号/行距/热区只允许定义在 `tokens.wxss`）。
- CI 流程见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。

## 注意事项

- `project.config.json` 为共享配置（已纳入版本库），个人配置 `project.private.config.json` 已加入 `.gitignore`。
- 密钥类变量（`JWT_SECRET`、`WECHAT_APP_SECRET`、`FARM_API_KEY` 等）只写入本地 `.env`，不要提交到仓库。
- `WECHAT_APP_SECRET` 未配置时登录走 mock，不影响其余接口调试。
- 生产上线前：将 `BASE_URL` 替换为 HTTPS 域名、配置微信 request 合法域名、把 `ENV` 置为 `production` 并将 `GATE_MODE` 切到 `enforce`，同时补全 `.env` 中的密钥。

## 相关文档

- 任务总表：[docs/tasks/README.md](docs/tasks/README.md)
- 全局约定与执行规范：[docs/tasks/00-全局约定与执行规范.md](docs/tasks/00-全局约定与执行规范.md)
- 产品说明书与技术实现方案见 `docs/` 目录。
