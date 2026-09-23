# 话农点金（HNDJ）

面向农户与农技人员的微信小程序，提供**农事播报、环境监测、AI 农技诊断问答**等能力。小程序端采用原生微信小程序 + TypeScript，后端采用 FastAPI，AI 诊断基于 **LangGraph 多轮编排 + MCP 工具层**，对接 DeepSeek 大模型。

![系统架构](docs/photo/architecture.svg)

## 技术栈

| 层 | 技术 |
| --- | --- |
| 小程序端 | 原生小程序 · TypeScript · glass-easel · 自定义 tabBar |
| 后端 | Python 3.11+ · FastAPI · SQLAlchemy 2.0 · Alembic |
| 数据 | MySQL 8.0 · Redis（可选）· SQLite（AI 断点记忆） |
| AI | LangGraph · MCP（工具层）· DeepSeek-v4-flash |

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
wx-mini/
├── miniprogram/          # 微信小程序前端
│   ├── pages/            # 页面（login/profile/index/ai/history/mine/env）
│   ├── api/              # 后端接口封装（ai/auth/farmer/...）
│   ├── utils/            # 请求、鉴权 guard、常量、定位
│   ├── types/            # 接口类型定义
│   └── app.json          # 页面/权限/tabBar 配置
├── backend/              # FastAPI 后端
│   ├── app/
│   │   ├── agent/        # LangGraph 诊断编排（状态机/节点/checkpointer）
│   │   ├── tools/        # MCP 工具层（传感器/天气/档案/知识库）
│   │   ├── ai_router/    # 垂类模型路由（水稻/烟草/通用）
│   │   ├── broadcast/    # 农事播报生成（规则/农事历/AI 兜底）
│   │   ├── services/     # 业务服务层
│   │   ├── api/          # 路由 + 鉴权依赖
│   │   ├── models/       # SQLAlchemy 模型
│   │   ├── integrations/ # 外部集成（微信/天气/逆地理/存储）
│   │   └── core/         # 配置/异常/响应/安全
│   ├── alembic/          # 数据库迁移
│   ├── tests/            # pytest 测试
│   └── requirements.txt
├── docs/                 # 设计与对接文档
└── project.config.json   # 小程序项目配置
```

## 快速开始

### 1. 后端

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

# 初始化数据库（MySQL 需先建库 huanong）
alembic upgrade head

# 启动服务
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：

- 接口文档（Swagger UI）：<http://127.0.0.1:8000/docs>
- ReDoc：<http://127.0.0.1:8000/redoc>

### 2. 小程序端

1. 用微信开发者工具导入项目根目录 `wx-mini`。
2. 后端地址在 [miniprogram/utils/config.ts](miniprogram/utils/config.ts) 中配置（默认 `http://127.0.0.1:8000`）。
3. 本地联调时在开发者工具「详情 → 本地设置」勾选「不校验合法域名」。

## 环境变量

核心配置见 [backend/.env.example](backend/.env.example)，常用项：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | MySQL 连接串（`mysql+pymysql://root:pwd@localhost:3306/huanong`） |
| `REDIS_URL` | Redis 地址，留空则用进程内内存做 token 会话 |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 小程序凭证（secret 缺失时登录走 mock） |
| `FARM_LLM_API_URL` / `FARM_API_KEY` | DeepSeek 兼容 API 地址与密钥 |
| `USE_MOCK_LLM` | `true` 时 AI 使用 mock 回复（联调用） |
| `LLM_DEFAULT_MODEL` | 默认模型（`deepseek-v4-flash`） |
| `CHECKPOINT_DB_URL` | AI 断点/短期记忆 SQLite 地址 |
| `MCP_ENABLED` | 是否启用环境取证工具层 |

## API 接口

统一响应结构：`{ "code": 200, "message": "success", "data": ... }`，除登录外均需 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | 微信登录 |
| GET/POST | `/api/farmer/profile` | 获取 / 保存农户档案 |
| GET | `/api/location/reverse-geocode` | 逆地理编码 |
| POST | `/api/location/set` | 设置农户位置 |
| GET | `/api/broadcast/today` | 今日农事播报 |
| POST | `/api/broadcast/mark-done` | 标记播报项完成 |
| POST | `/api/broadcast/preview` | 实时生成播报（不落库，联调调试用） |
| GET | `/api/environment/summary` | 环境监测汇总 |
| GET | `/api/environment/detail` | 环境监测详情 |
| POST | `/api/ai/chat` | AI 农技诊断对话 |
| GET | `/api/ai/history` | 对话历史 |
| DELETE | `/api/ai/history/{session_id}` | 删除对话 |
| POST | `/api/user/avatar/upload` | 上传头像 |
| DELETE | `/api/user/cancel` | 注销账户 |

## AI 农技诊断

诊断链路为「证据采集 → 诊断 → 开方 → 回访」的闭环，基于 LangGraph 状态机实现：

![AI 诊断状态机](docs/photo/ai-diagnosis-flow.svg)

- **模型路由**：根据 `cropTag` 分发到水稻 / 烟草 / 通用垂类模型（见 `app/ai_router/`）。
- **置信度分级**：依据「图片 / 症状 / 管理 / 环境」证据信号打分，划分 high / medium / low。
- **环境取证（MCP 工具层）**：`app/tools/` 提供传感器、天气、档案、知识库四类工具。
- **记忆**：短期用 SQLite checkpointer 持久化断点续聊，长期用 `memory_service` 存取画像与关键事实。

## 农事播报

播报生成采用**规则优先**策略，规则未命中时逐级兜底，保证任何环境下都能产出当日提醒：

![农事播报生成链路](docs/photo/broadcast-flow.svg)

- **环境快照**：`EnvSnapshot`（`app/broadcast/snapshot.py`）统一收敛设备读数、天气与档案数据源，缺失字段逐级兜底。
- **规则集**：阈值规则（高温/低温/墒情/高湿/降雨/大风）与病虫害气象风险（温湿组合判断）。
- **农事历**：按「作物 + 生育期」补充阶段农事提醒。
- **AI 兜底**：规则均未命中时，复用通用农业模型生成 150 字内简报。

## 测试

```bash
cd backend
.venv\Scripts\python.exe -m pytest -q     # Windows
python -m pytest -q                      # macOS / Linux
```

测试用 SQLite 内存库 + mock LLM 隔离环境，不依赖 MySQL / Redis / 真实大模型。

## 注意事项

- `project.config.json` 为共享配置（已纳入版本库），`project.private.config.json` 为个人配置（已加入 `.gitignore`）。
- `WECHAT_APP_SECRET` 未配置时登录走 mock，不影响其余接口调试。
- 生产上线前：将 `BASE_URL` 替换为 HTTPS 域名、配置微信 request 合法域名，并补全 `.env` 中的密钥。
