# T6-07 Langfuse 私有化部署与提示词版本对比

## 这一项要做什么

私有化部署 Langfuse 作为 LLM 专用观测后端，并把 T6-06 产生的 generation span 上报到它。选 Langfuse 而非云端 SaaS 的原因是本项目的合规底线："敏感农事数据不外流"（17.2 风险表）。Langfuse 支持完全私有化部署，其部署栈与云版本一致，可运行在自有基础设施乃至隔离集群中；它本身基于 OpenTelemetry，能把 span 上报到自建的 collector。本项另交付提示词版本对比能力：提示词版本化后，模型质量是否变好必须有可比较的证据，而不是"改完感觉好一些"。

## 前置依赖

- 依赖 T6-06 已提供：`traced_node`、`traced_generation` 与 generation span 的 `gen_ai.*` / `llm.*` 属性键；依赖 `Settings.OTEL_ENDPOINT` 与导出器配置。
- 依赖 T2-60 已提供：提示词模板组织与版本管理，提示词存库并带版本号；依赖 T2-02、T2-03：流程配置版本与热加载。
- 依赖 T2-59：token 计量与成本，generation span 的 token 与成本字段来源。
- 依赖 T1-04：Alembic 迁移链路，若在业务库记录 `prompt_version` 需迁移（本项不新增业务表，复用 `llm_call` 与 `consultation`）。
- 外部条件：一台可跑 Docker Compose 的服务器，建议至少 4 核 16 GiB 内存与 100 GiB 存储（观测数据体量大）；需可访问镜像仓库；`ENCRYPTION_KEY` 用 `openssl rand -base64 32` 生成，`SALT` 与 `NEXTAUTH_SECRET` 为随机长字符串。
- 待确认：部署形态（与业务同一台机还是独立隔离集群）。本项按"同一 Compose 网络内、对外不暴露端口"实现，独立集群只需把 `LANGFUSE_HOST` 指向另一地址。
- 现有起点：`backend/app/` 下无 `observability/langfuse.py`，本项新建。

## 参考章节

- 技术方案 第14章 14.4 可观测性实现（技术栈："OpenTelemetry（链路追踪）+ Langfuse（LLM 专用观测，私有化部署）+ structlog（结构化日志）"；"Langfuse 支持完全私有化部署，其部署栈与云版本一致，可运行在自有基础设施乃至隔离集群中；同时它基于 OpenTelemetry，可以把自身的 span 上报到自建的 collector。这与本项目'敏感农事数据不外流'的要求一致"）
- 技术方案 第15章 15.1 部署拓扑（阶段一单机 Compose 的组件清单含 `langfuse`；"阶段二云上容器服务"）
- 技术方案 第15章 15.5 版本与发布（"提示词版本化：提示词模板存库并版本化"；"灰度发布：……模型先小流量"；"可复现性是审计的前提"）
- 技术方案 第17章 17.2 产品与合规风险（"农事数据外流 → 境内云 API 为主 + 私有化兜底；可观测性私有化部署（14.4）"）
- 全局约定 第七节 环境与工具（容器：Docker + Docker Compose）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `deploy/langfuse/docker-compose.langfuse.yml` | Langfuse 及其依赖（postgres、redis、clickhouse、minio）的 Compose 片段 |
| 新增 | `deploy/langfuse/.env.langfuse.example` | `SALT`、`ENCRYPTION_KEY`、`NEXTAUTH_SECRET`、`NEXTAUTH_URL` 模板 |
| 新增 | `deploy/langfuse/README.md` | 部署步骤、升级方式、端口与网络策略 |
| 新增 | `app/observability/langfuse.py` | `LangfuseExporter`：把 generation span 上报到自建 collector；`prompt_version` 注入 |
| 修改 | `app/observability/tracing.py` | 增加 `langfuse` 导出器分支（与 `otlp` 并列） |
| 修改 | `app/core/config.py` | 新增 `LANGFUSE_ENABLED`、`LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_TRACE_RATIO` |
| 修改 | `backend/.env.example` | 补上述变量与注释 |
| 修改 | `app/llm/gateway.py` | generation span 写入 `prompt_version`、`prompt_name`、`prompt_is_fallback` 属性 |
| 新增 | `deploy/langfuse/scripts/prompt_compare.py` | 提示词版本对比脚本：按版本聚合 generation |
| 新增 | `backend/tests/observability/test_langfuse.py` | 上报开关、失败不影响业务、属性完整性 |

## 实现要点

### 一、私有化部署的合规理由与网络策略

本项目的红线是农事数据不外流（17.2）。LLM 观测天然要记录提示词与模型输出，如果用的是云端 SaaS，等于把农事数据送出境外的第三方，与"境内云 API 为主 + 私有化兜底"的模型策略直接冲突。因此 Langfuse 必须私有化，且部署时要满足：

- **不对外暴露端口**：Langfuse 的 web 端口只在 Compose 网络内可达，管理界面通过 Caddy（T6-10）内网反代或 SSH 隧道访问，不映射到公网。T6-10 的 Compose 里 `langfuse` 不发布 `ports`，只加 `expose`。
- **观测数据留存可控**：Langfuse 自带 Postgres 与对象存储，数据落在自有磁盘，可设保留期。与业务库是否同库是两件事——观测数据量级远大于业务数据，必须独立存储，不能塞进业务的 PostgreSQL（这也是 15.1 把 `langfuse` 单列的原因）。
- **升级方式确定**：`docker compose up --pull always` 拉新镜像，数据在卷里，升级不改数据。升级不得影响业务：Langfuse 不可用时业务照常，因此导出必须是非阻塞且可失效的。

### 二、部署要点

用官方仓库的 Compose 文件做基础，本项目只做三处修改并记录在 `README.md`：

1. **覆盖密钥**：`SALT`（至少 32 字符）、`ENCRYPTION_KEY`（`openssl rand -base64 32`）、`NEXTAUTH_SECRET`。官方说明这是"测试可跳过、存敏感数据必须改"的项，而本项目存的是真实农事数据，不可跳过。
2. **资源与卷**：给 `langfuse-web` 与 `clickhouse` 设内存上限，给数据库与对象存储挂命名卷，避免与业务容器争抢（单机 Compose 阶段共享同一台机，这是最实际的冲突点）。
3. **上报到自建 collector**（可选）：官方支持通过环境变量把 Langfuse 自身 span 送到外部 collector，用于把 Langfuse 的运维指标也纳入统一 APM。

```yaml
# deploy/langfuse/docker-compose.langfuse.yml（节选，示意）
services:
  langfuse-web:
    image: langfuse/langfuse:3
    expose: ["3000"]                 # 不发布到宿主，仅 Compose 网络内可达
    environment:
      - SALT=${LANGFUSE_SALT}
      - ENCRYPTION_KEY=${LANGFUSE_ENCRYPTION_KEY}
      - NEXTAUTH_SECRET=${LANGFUSE_NEXTAUTH_SECRET}
      - NEXTAUTH_URL=${LANGFUSE_NEXTAUTH_URL}
      - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_COLLECTOR_ENDPOINT:-}   # 留空则不上报自身
      - OTEL_SERVICE_NAME=langfuse-web
    mem_limit: 2g
```

`expose` 而非 `ports` 是本项最容易做错的一处：官方文档演示用 `ports: 3000:3000` 方便本地调试，直接照抄会把无鉴权界面的观测后端暴露到公网。

### 三、上报接入

```python
# app/observability/langfuse.py
class LangfuseExporter:
    """把 generation span 上报到私有 Langfuse。失败静默降级，不影响问诊。"""
    def __init__(self, settings: Settings) -> None:
        self._client = Langfuse(
            host=settings.LANGFUSE_HOST,
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            tracing_enabled=settings.LANGFUSE_ENABLED,
        ) if settings.LANGFUSE_ENABLED else None

    def trace_ratio(self) -> float: ...
```

关键约束：

- **抽样比由 `LANGFUSE_TRACE_RATIO` 控制**（默认本地 1.0、生产建议 0.2）。LLM 观测数据体量远大于普通 trace，全量上报在小内存机器上会拖垮 Langfuse 自身。抽样在 span 创建时决定，保证同一 trace 的 span 同进同出，不出现"父在子不在"的残缺 trace。
- **导出器不可用时静默降级**：`LANGFUSE_ENABLED=false` 或客户端构造失败都不得抛错到业务链路。与 T6-06 的 OTel 导出器同一原则：观测是旁路，不能成为可用性依赖。
- **`prompt_version` 必须写入 generation span**，与 T2-60 的版本号一致；同时写 `prompt_name`（模板名，如 `interviewer.teal`）与 `prompt_is_fallback`（是否走了降级模板）。后两者是版本对比能按"环节+版本"聚合的前提，缺了就只能按全局版本看，无法定位"哪个环节的提示词退化了"。

### 四、提示词版本对比的用法

对比脚本读 Langfuse 的 generation 记录，按 `prompt_name` + `prompt_version` 聚合，输出每个版本的质量与成本对比：

```python
# deploy/langfuse/scripts/prompt_compare.py
GROUPS = ("prompt_name", "prompt_version", "llm.model")
METRICS = ("latency_p50_ms", "latency_p95_ms", "tokens_p50",
           "contract_violation_rate", "gate_block_rate", "quality_label_bad_rate")
```

六个指标的选择理由要写进脚本注释：前三个是成本与延迟（改提示词常伴随 token 增长），后三个是质量。`contract_violation_rate`（解析失败占比）是提示词变更最灵敏的负面信号——提示词让模型更容易输出结构化格式之外的文本时，这个值立刻上升，而它无需人工标注即可计算。`gate_block_rate` 与 `quality_label_bad_rate`（来自 T6-09 的人工抽检标注）才是真正的质量口径。

对比脚本的输出用于三个决策：新提示词能否全量（`quality_label_bad_rate` 不升且 `contract_violation_rate` 不升）、是否需要回退（T6-09 的触发条件）、哪个环节值得继续优化。**版本对比与 15.5 的"提示词版本化"是一件事的两面**：没有版本号，对比无从谈起；没有对比，版本化只是存档。

注意一个边界：`prompt_snapshot` 记录生成当时的提示词版本（15.5、T3-01），那是业务侧的可复现性证据；Langfuse 的版本对比是研发侧的优化依据。两者都写 `prompt_version` 且必须同源，否则会出现"业务说用的是 v7、观测数据显示 v5"这类无法对账的情况。本项要求该字段来源唯一：都取 T2-60 的提示词注册表。

### 五、部署与业务的关系

阶段一（15.1）`langfuse` 与业务在同一个 Compose 文件里但独立服务，可以 `docker compose up langfuse` 单独启动；业务对 Langfuse 的依赖是单向且弱依赖——Langfuse 停掉，业务照常跑，只是没有观测数据。T6-10 的 Compose 里要把这一点写成启动顺序注释：业务服务不设 `depends_on: langfuse`，避免观测后端故障拖垮发布。

## 验收标准

- `docker compose -f deploy/langfuse/docker-compose.langfuse.yml up -d` 后，`docker compose ps` 显示 langfuse-web 与其依赖容器均为 running，web 容器日志出现 `Ready`。
- `docker compose port langfuse-web 3000` 无输出（未发布到宿主），证明界面未对外暴露；从宿主用 Compose 网络内的服务名可访问。
- `deploy/langfuse/.env.langfuse.example` 中存在 `LANGFUSE_SALT`、`LANGFUSE_ENCRYPTION_KEY`、`LANGFUSE_NEXTAUTH_SECRET`、`LANGFUSE_NEXTAUTH_URL` 四项且无默认弱值；`README.md` 写明 `ENCRYPTION_KEY` 用 `openssl rand -base64 32` 生成。
- `python -m pytest backend/tests/observability/test_langfuse.py` 通过：`LANGFUSE_ENABLED=false` 时上游不发任何请求；`LANGFUSE_ENABLED=true` 但 Langfuse 不可达时问诊仍返回 200，错误仅记为日志与指标。
- generation span 上存在 `prompt_name`、`prompt_version`、`prompt_is_fallback` 三个属性；`prompt_version` 与 `app/agent/prompts/` 注册表当前版本一致（用例从注册表取期望值比对）。
- `LANGFUSE_TRACE_RATIO=0.2` 时，跑 100 次问诊上报的 trace 数在 10 至 30 之间（允许抽样波动），且每条上报的 trace 内 span 完整（无父 span 缺失）。
- 在 Langfuse 界面按 `prompt_name` 过滤可看到同一环节的多个 `prompt_version`；用两个版本的记录跑 `python deploy/langfuse/scripts/prompt_compare.py`，输出含六个指标且按版本分组。
- `docker compose -f deploy/langfuse/docker-compose.langfuse.yml down` 后重启业务容器，问诊接口仍返回 200（弱依赖成立）。
- `deploy/langfuse/README.md` 中写明升级命令 `docker compose up --pull always` 与升级不影响业务的说明；写明观测数据落在哪些命名卷上。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。
