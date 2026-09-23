# T6-10 Docker Compose 部署编排

## 这一项要做什么

把后端运行所需的六个组件编排成一套单机 Docker Compose：`api`、`worker`、`postgres`、`redis`、`langfuse`、`caddy`。目标是消除"开发机能跑、服务器缺组件"这类环境差异，让开发与预发一致（4.2 技术栈的说明）。本项只做阶段一（验证期）的单机编排，并在这份文件里写清阶段二（云上容器服务）的演进条件，避免执行者提前上 K8s。

## 前置依赖

- 依赖 T1-01 已提供：`requirements.txt` 与 `pyproject.toml` 基线、Python 版本约束。
- 依赖 T1-04 已提供：Alembic 迁移链路，容器启动时用 `alembic upgrade head` 而非自动建表（"自动建表会导致开发机能跑、生产缺字段"，15.1 与 16.2 都列为硬要求）。
- 依赖 T1-03：PostgreSQL 镜像需含 `pgvector` 与 `TimescaleDB` 扩展，且库初始化脚本已建 `env_reading` 时序表。
- 依赖 T1-08：环境隔离与生产启动自检，容器以 `ENV=production` 启动时会做密钥非空、不使用 mock LLM、`GATE_MODE` 不为 `shadow` 等检查。
- 依赖 T1-13：`event_outbox` 投递进程的运行载体（worker 或独立进程）。
- 依赖 T6-07：Langfuse 的 Compose 片段与 `.env.langfuse.example`；本项把它并入主 `docker-compose.yml`。
- 外部条件：一台可跑 Docker 与 Docker Compose 的 Linux 服务器；域名与证书（caddy 自动申请 ACME 证书需域名解析到本机且 80/443 可达）；需确认是否已有外部反向代理。
- 待确认：反向代理选型。技术方案 15.1 部署拓扑写 `caddy`（自动 HTTPS），4.2 完整技术栈写 `Nginx`（TLS、域名策略、静态资源），两处不一致，本项按 15.1 的部署拓扑以 `caddy` 实现并在回报中说明。
- 现有起点：仓库根目录无 `docker-compose.yml`、无 `Dockerfile`、无 `deploy/` 目录（除 T6-07 新建的 `deploy/langfuse/`）。

## 参考章节

- 技术方案 第15章 15.1 部署拓扑（"演进路径：单机 Docker Compose → 云上容器服务。不一开始上 K8s——项目尚在验证期，K8s 的运维成本会拖慢迭代"；阶段一组件清单六个；阶段二"`api` 与 `worker` 独立扩缩容，因为两者负载特征完全不同"；"数据库不做读写分离"）
- 技术方案 第4章 4.2 完整技术栈（容器："Docker + Docker Compose | 新增 | 开发与预发一致"；反向代理行）
- 技术方案 第16章 16.2 第一步（"清理启动时自动建表逻辑，全部改为 Alembic 显式迁移。这是硬要求"）
- 技术方案 第7章 7.5 检查点实现（连接池参数与独立 schema）
- 全局约定 第七节 环境与工具（容器、数据库扩展、Redis、ARQ）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `docker-compose.yml` | 六个服务的完整编排 |
| 新增 | `docker-compose.override.example.yml` | 本地开发覆盖示例（挂载源码、关闭 caddy 证书） |
| 新增 | `backend/Dockerfile` | 后端镜像：多阶段构建，非 root 用户，含 alembic |
| 新增 | `backend/.dockerignore` | 排除 `.env`、`tests/`、`__pycache__`、本地 `agent_checkpoints.db` |
| 新增 | `deploy/postgres/init/01_extensions.sql` | `CREATE EXTENSION vector / timescaledb` 与 `checkpoint` schema |
| 新增 | `deploy/caddy/Caddyfile` | 域名、反向代理、TLS 自动申请、内部界面限制 |
| 新增 | `deploy/api/entrypoint.sh` | 先 `alembic upgrade head` 再起 uvicorn 的入口脚本 |
| 新增 | `deploy/worker/arq_worker.py` | ARQ worker 启动入口（触达、快照、知识构建、指标采集、抽检） |
| 新增 | `deploy/README.md` | 启动顺序、健康检查、备份与升级说明 |
| 修改 | `backend/.env.example` | 补 Compose 相关变量（`ENV`、`POSTGRES_*`、`GATE_MODE`、`OTEL_*`） |

## 实现要点

### 一、docker-compose.yml 完整骨架

```yaml
# docker-compose.yml（阶段一：单机 Compose，六服务）
name: huanong

x-api-env: &api_env
  ENV: ${ENV:-staging}
  DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
  CHECKPOINT_DB_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
  REDIS_URL: redis://redis:6379/0
  GATE_MODE: ${GATE_MODE:-shadow}
  USE_MOCK_LLM: ${USE_MOCK_LLM:-true}
  OTEL_ENABLED: ${OTEL_ENABLED:-false}
  OTEL_EXPORTER: ${OTEL_EXPORTER:-otlp}
  OTEL_ENDPOINT: ${OTEL_ENDPOINT:-http://langfuse:4318}
  LANGFUSE_HOST: ${LANGFUSE_HOST:-http://langfuse-web:3000}

services:
  postgres:
    image: timescale/timescaledb-ha:pg16        # 自带 timescaledb，另装 pgvector
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - pgdata:/home/postgres/pgdata/data
      - ./deploy/postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    volumes: [redisdata:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10
    restart: unless-stopped

  api:
    build: { context: ./backend, dockerfile: Dockerfile }
    command: ["deploy/api/entrypoint.sh"]        # 迁移后起 uvicorn（4 workers）
    env_file: [.env]
    environment: *api_env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    expose: ["8000"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 6
    restart: unless-stopped

  worker:
    build: { context: ./backend, dockerfile: Dockerfile }
    command: ["python", "-m", "deploy.worker.arq_worker"]
    env_file: [.env]
    environment: *api_env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped

  langfuse:
    extends: { file: deploy/langfuse/docker-compose.langfuse.yml, service: langfuse-web }
    # 不发布端口，仅 Compose 网络内可达；业务不 depends_on 它（弱依赖）

  caddy:
    image: caddy:2-alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddydata:/data
      - caddyconfig:/config
    depends_on:
      api: { condition: service_healthy }
    restart: unless-stopped

volumes: { pgdata: {}, redisdata: {}, caddydata: {}, caddyconfig: {} }
```

关键点逐条：

- **`api` 不发布端口，只有 `expose`**：外部入口唯一，避免绕过 caddy 直连 8000 端口。这是"入口收敛"的工程手段，不是风格问题。
- **`api` 与 `worker` 不设 `depends_on: langfuse`**：可观测是弱依赖（T6-07），Langfuse 故障不得阻止发布。
- **`worker` 不设 `ports` 也不设健康检查**：它是后台消费进程，没有 HTTP 接口；健康由"进程存活 + 队列积压"两个观察项覆盖（见验收标准）。
- **`GATE_MODE` 默认 `shadow`**：与 T2-46 一致，灰度期用影子模式；切生产时由环境变量覆盖为 `enforce`，并由 T1-08 的启动检查保证生产不允许 `shadow`。
- **`ENV` 默认 `staging`**：默认值不能是 `production`，否则本地起容器会因缺少微信密钥而直接退出。

### 二、阶段一与阶段二，以及 api 与 worker 为什么独立扩缩容

**阶段一是单机 Compose**，理由（15.1）：项目尚在验证期，K8s 的运维成本会拖慢迭代。上 K8s 的条件不是"访问量变大"，而是"单机 Compose 的能力具体不够用了"。可观察的触发条件写进 `deploy/README.md`：任意一项达成即进入阶段二评估——单机 CPU 或内存持续打满、需要多机容灾、需要在不停服前提下滚动发布、需要按服务独立扩缩容。

**阶段二才是云上容器服务**，`api` 与 `worker` 独立扩缩容。理由（15.1）：两者负载特征完全不同。`api` 是短时高频，受问诊并发驱动（白天农户使用高峰）；`worker` 是定时批量（每日快照、跨日弱化、知识构建、指标采集、质量抽检），受调度驱动（深夜集中跑）。混在一起会导致"夜里快照任务把白天问诊的 CPU 吃光"——反过来也一样，白天问诊高峰会挤占定时任务的执行窗口，使跨日弱化（T3-10）这类与时点强相关的任务错过窗口。这份 Compose 因此从一开始就拆成两个服务、两个命令入口，使阶段二的独立扩缩容不需要改代码，只需拆部署单元。

`worker` 的定时任务清单（与 `arq_worker.py` 的注册保持一致）：快照生成（T3-08）、回访到期扫描（T3-09）、跨日弱化（T3-10）、触达发送与重试（T3-12）、知识库索引构建（T2-48）、指标采集（T6-09）、质量抽检（T6-09）、评测集回归（T6-01，可选，日常走 CI 不走近端）。

### 三、为什么不做数据库读写分离

15.1 明确：本项目读多写少且数据量级可控，读写分离带来的主从延迟会引入"刚下的处方卡查不到"这类难以排查的问题，收益不成正比。这句要落到实现约束上：

- `DATABASE_URL` 只有一条，不设 `DATABASE_REPLICA_URL`。若在配置里预留了只读地址，早晚会有人把某条查询指过去，于是"写后立刻读"的链路（开方后立即返回处方卡、写入快照后立即返回）会偶发查不到数据。
- 处方卡与检查点同库同事务（4.3 第二条）：检查点说已开方而业务表没有处方卡的不一致，在主从架构下无法用同事务消除。
- 扩展手段改为垂直扩容与索引优化，而不是加只读副本。`deploy/README.md` 里写明这条，避免运维按常规经验加主从。

### 四、启动、迁移与健康检查

`entrypoint.sh` 的顺序固定为：等待数据库就绪（`pg_isready` 循环）→ `alembic upgrade head` → 启动 uvicorn（4 workers）。三处约束：

- **迁移只在 `api` 启动时执行一次**，`worker` 不执行迁移（否则多进程并发跑迁移）。多副本 `api` 时需改为独立的迁移作业（阶段二的典型改造点，写进 README）。
- **不使用 `--reload`**，生产配置为 uvicorn 4 workers（15.1 拓扑注释），且不使用 Gunicorn（4.2 提到"生产配 Gunicorn 多 worker"是备选表述；本项按 15.1 的 uvicorn 实现，回报中说明该不一致）。
- **健康检查走 `/health`**，该接口只返回进程与数据库连通性，不检查模型服务（模型不可用时应降级而非判不健康，否则一次模型抖动会把所有副本摘除）。

### 五、Caddy 与内部界面

```caddyfile
# deploy/caddy/Caddyfile
{$API_DOMAIN} {
    reverse_proxy api:8000
    encode gzip
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        -Server
    }
}

{$METRICS_DOMAIN} {
    @internal remote_ip {$LAN_CIDR}
    handle @internal { reverse_proxy api:8000 }     # 指标接口限内网
    respond 403
}
```

Langfuse 界面不在这份 Caddyfile 里暴露子域，通过内网或 SSH 隧道访问（T6-07 的要求）。TLS 由 caddy 自动申请，需要 `API_DOMAIN` 已解析到本机；无域名时用 `tls internal`（自签）并把证书分发给客户端，写进 `docker-compose.override.example.yml`。

## 验收标准

- `docker compose config` 返回 0，无未解析的变量告警；六个服务（`api`、`worker`、`postgres`、`redis`、`langfuse`、`caddy`）全部出现在输出中。
- `docker compose up -d` 后 `docker compose ps` 显示 `postgres`、`redis`、`api` 三个服务为 healthy，`worker`、`caddy`、`langfuse` 为 running。
- `docker compose exec api alembic current` 显示的版本号等于 `alembic heads`；`docker compose logs api` 中迁移日志先于 uvicorn 启动日志出现。
- `docker compose port api 8000` 无输出（未对外发布）；`docker compose exec caddy wget -qO- http://api:8000/health` 返回 200。
- `curl -sS https://$API_DOMAIN/health` 返回 200 且响应头含 `strict-transport-security`；直连 `http://$API_DOMAIN` 被 Caddy 301 重定向到 https。
- `docker compose port langfuse 3000` 无输出；`docker compose stop langfuse` 后重启 `api`，`/api/login` 仍返回 200（弱依赖）。
- `docker compose down; docker compose up -d` 后数据保留：`docker compose exec postgres psql -c "\dt"` 显示业务表仍在；`pgdata` 卷未被删除（不使用 `down -v`）。
- `deploy/postgres/init/01_extensions.sql` 生效：`docker compose exec postgres psql -c "SELECT extname FROM pg_extension"` 输出含 `vector` 与 `timescaledb`；`docker compose exec postgres psql -c "\dn"` 显示 `checkpoint` schema。
- 把 `ENV=production` 与 `GATE_MODE=shadow` 同时注入并重启 `api`，容器退出且日志含启动检查违规项（T1-08 生效），证明编排没有绕过自检。
- `worker` 的定时任务在容器内可见：`docker compose exec worker python -m deploy.worker.arq_worker --list-jobs` 输出包含快照生成与质量抽检两个任务名。
- `deploy/README.md` 写明启动顺序、`alembic upgrade` 的执行位置、阶段二触发条件、不做读写分离的理由、以及多副本时需把迁移拆为独立作业。
- 阶段二触发条件在 README 中以可观察项表述（CPU/内存持续打满、需要多机容灾、需不停服滚动发布、需按服务独立扩缩容），而非"访问量变大"这类模糊条件。
