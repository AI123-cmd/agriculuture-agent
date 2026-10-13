# 部署编排（T6-10）

单机 Docker Compose，把后端运行所需的组件编排成一套：`api`、`worker`、`postgres`、
`redis`、`caddy`，外加经网络互联的 `langfuse`（T6-07 的独立栈）。

阶段一（验证期）只做单机 Compose，**不上 K8s**：K8s 的运维成本会拖慢迭代，上它的条件是
"单机能力具体不够用了"，判据见第六节。

## 一、组件与端口

| 服务 | 镜像/来源 | 端口 | 职责 |
| --- | --- | --- | --- |
| `postgres` | `timescale/timescaledb-ha:pg16` | 不发布 | 业务库 + pgvector + TimescaleDB |
| `redis` | `redis:7-alpine` | 不发布 | 缓存、ARQ 队列、额度计数 |
| `api` | 本仓库 `backend/Dockerfile` | `expose 8000`（不发布） | FastAPI，4 workers |
| `worker` | 同上 | 无端口 | ARQ：发件箱投递、回访扫描、触达、指标采集 |
| `caddy` | `caddy:2-alpine` | `80`/`443` | 反向代理、自动 HTTPS、指标限内网 |
| `langfuse` | `deploy/langfuse/docker-compose.langfuse.yml` | 仅内网 | LLM 观测（弱依赖） |

**只有 caddy 发布端口**，其余服务都在 Compose 网络内。外部唯一入口是 Caddy——这是"入口
收敛"的工程手段：api 若直连宿主 8000，任何"限内网"访问控制都能被绕过。

## 二、启动顺序

```bash
# 0. 准备业务环境变量（含数据库口令、微信密钥、JWT_SECRET 等）
cp backend/.env.example backend/.env
#    至少确认：POSTGRES_PASSWORD、JWT_SECRET（≥32 字符）、API_DOMAIN
#    docker-compose.yml 从 backend/.env 读 POSTGRES_* 与 api/worker 的业务变量

# 1.（可选）先起 Langfuse 观测栈——它不是业务的启动前提，可后起
cp deploy/langfuse/.env.langfuse.example deploy/langfuse/.env
#    按 deploy/langfuse/README.md 生成 SALT / ENCRYPTION_KEY / NEXTAUTH_SECRET 并填写
docker compose -f deploy/langfuse/docker-compose.langfuse.yml \
  --env-file deploy/langfuse/.env up -d

# 2. 起业务栈
docker compose up -d

# 3. 观察状态：postgres / redis / api 应为 healthy，worker / caddy 为 running
docker compose ps
docker compose logs -f api
```

**顺序约束**：数据库先于迁移、迁移先于 uvicorn，这三步由 `deploy/api/entrypoint.sh`
在一次容器启动内串起来，人为分步没有意义。业务与 Langfuse 之间无顺序约束（弱依赖）。

## 三、迁移在哪里执行

`alembic upgrade head` **只在 `api` 容器启动时执行一次**（`deploy/api/entrypoint.sh`）。
`worker` 不执行迁移——两个进程同时跑迁移会在 `alembic_version` 上互相踩踏。

- 查看当前版本：`docker compose exec api alembic current`
- 查看代码期望版本：`docker compose exec api alembic heads`（应等于 `startup_check.EXPECTED_REVISION`）
- 排查迁移日志：`docker compose logs api | head`——迁移日志必先于 uvicorn 启动日志出现

**多副本 `api` 时**：容器 entrypoint 的"启动即迁移"会变成并发迁移。此时必须把迁移拆成
独立的一次性作业（如 `docker compose run --rm api alembic upgrade head`），再滚动起副本。
这是阶段二（下节）的典型改造点。

迁移不使用 `create_all`：自动建表会导致"开发机能跑、生产缺字段"（技术方案 16.2 的硬要求）。

## 四、健康检查与探活

- `api` 的容器 healthcheck 打 `GET /health`：该端点只报**进程 + 数据库连通性**，不检查模型
  服务、知识库与 Langfuse。它们都是弱依赖，抖动时应降级而非判不健康——否则一次模型抖动会
  让编排把所有副本摘除，把局部降级放大成整体不可用。
- `caddy` 的 `depends_on: api: service_healthy`：等 api 健康再挂上，避免首次访问 502。
- `worker` **没有** healthcheck：它是后台消费进程，没有 HTTP 接口。它的健康用两个观察项覆盖：
  1. 进程存活：`docker compose ps worker` 为 running；
  2. 队列积压：`docker compose exec redis redis-cli LLEN arq:queue` 不持续增长。

## 五、Caddy 与 TLS

`deploy/caddy/Caddyfile` 两个站点：

- `{$API_DOMAIN}`：业务 API，自动申请 ACME 证书（需域名已解析到本机、80/443 可达），
  回写 HSTS 并去掉 `Server` 头。
- `{$METRICS_DOMAIN}`：指标接口，用 `remote_ip {$LAN_CIDR}` 限内网，其余 `respond 403`。
  指标暴露全平台经营口径（T6-09），网络层再收一道，与应用层鉴权（`require_review_operator`）
  互补而非替代。

Langfuse 界面**不**在本 Caddyfile 里暴露子域：通过内网或 SSH 隧道访问（T6-07 的要求）。

无域名时用 `docker-compose.override.example.yml`（改用 `tls internal` 自签证书、只映射
8080 用于本地验证）。

## 六、阶段二触发条件（何时才上云上容器服务）

不是"访问量变大"这类模糊条件——**下列任一可观察项达成**即进入阶段二评估：

1. 单机 CPU 或内存**持续**打满（不是尖峰）；
2. 需要多机容灾（单台宕机即整体不可用，业务无法接受）；
3. 需要在不停服前提下滚动发布；
4. 需要按服务独立扩缩容。

阶段二把 `api` 与 `worker` 拆成独立扩缩容单元。理由（技术方案 15.1）：两者负载特征完全不同
——`api` 是短时高频（受白天问诊并发驱动），`worker` 是定时批量（深夜快照、跨日弱化、指标
采集）。混在一起会"夜里快照把白天问诊的 CPU 吃光"，或"白天问诊挤占跨日弱化的执行窗口"。
本 Compose 从一开始就拆成两个服务、两个命令入口，使阶段二只需拆部署单元，不改代码。

## 七、为什么不做数据库读写分离

技术方案 15.1 明确：本项目读多写少、数据量级可控，读写分离的主从延迟会引入"刚下的处方卡
查不到"这类难排查问题，收益不成正比。落到实现约束：

- `DATABASE_URL` **只有一条**，不设 `DATABASE_REPLICA_URL`。预留只读地址早晚会有人把某条
  查询指过去，于是"写后立刻读"（开方后立即返回处方卡、写快照后立即返回）会偶发查不到。
- 处方卡与检查点同库同事务（4.3 第二条）：主从架构下无法用同事务消除"检查点说已开方而业务表
  没有处方卡"的不一致。
- 扩展手段是**垂直扩容 + 索引优化**，不是加只读副本。

运维若要按常规经验加主从，请先回到本条。

## 八、备份与升级

```bash
# 备份业务库（不删卷的日常备份）
docker compose exec postgres pg_dump -U "${POSTGRES_USER:-huanong}" "${POSTGRES_DB:-huanong}" > backup.sql

# 升级业务栈（先拉新镜像再重建；数据在命名卷里，升级不改数据）
docker compose pull
docker compose up -d

# 升级 Langfuse（独立栈）
docker compose -f deploy/langfuse/docker-compose.langfuse.yml \
  --env-file deploy/langfuse/.env up --pull always -d
```

**禁止 `docker compose down -v`**：`-v` 会删除命名卷（`pgdata`/`redisdata`/`uploads`），
等于清库。重启用 `down`（不带 `-v`）或 `restart`，数据保留在卷里。

升级前备份 `backend/.env` 与 Langfuse 的 `ENCRYPTION_KEY` / `SALT`：前者含业务密钥，
后者更换会导致 Langfuse 已存的加密字段无法解密。

## 九、Langfuse 与业务的关系（弱依赖）

- 业务服务**不** `depends_on` Langfuse：观测后端故障不得阻断发布。它们经命名网络
  `huanong-langfuse` 互联（`docker-compose.yml` 的 `networks.langfuse`）。
- Langfuse 未启动时，业务照常起（Compose 按名创建网络），只是没有观测数据；`LANGFUSE_HOST`
  解析失败时上报端静默降级。
- 验证：`docker compose stop` 掉 Langfuse 后重启 `api`，`/api/login` 仍应返回 200。

## 十、与 T6-10 任务文档的两处偏离（说明）

1. **`langfuse` 不并入本文件**：T6-07 的 Langfuse 是自包含六服务栈，只 `extends` 一个 web
   服务会让其余依赖缺失、命名卷被改名（孤立旧数据）、并被迫给密钥补默认值（拆掉 fail-fast）。
   故它仍由自己的文件起停，两栈经网络互联。详见 `docker-compose.yml` 顶部注释。
2. **反向代理选 Caddy 而非 Nginx**：技术方案 15.1 写 `caddy`、4.2 写 `Nginx`，两处不一致；
   本项按 15.1 的部署拓扑实现。
