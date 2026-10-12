# Langfuse 私有化部署（T6-07）

把 LLM 专用观测后端跑在本项目自己的基础设施上，接收 T6-06 产生的 generation span。

## 一、为什么必须私有化

本项目的合规红线是**农事数据不外流**（技术方案 17.2）。LLM 观测天然要记录提示词与模型
输出，用云端 SaaS 等于把真实农事数据送出国境的第三方，与"境内云 API 为主 + 私有化兜底"
的模型策略直接冲突。因此：

- **界面不对外暴露**：web 端口只在 Compose 网络内可达，通过 Caddy（T6-10）内网反代或
  SSH 隧道访问；
- **数据留存可控**：观测数据落在自有磁盘的命名卷上，与业务库完全分开——观测数据量级远
  大于业务数据，塞进业务 PostgreSQL 会拖垮它（这也是技术方案 15.1 把 `langfuse` 单列的
  原因）；
- **可脱离业务独立起停**：业务是**弱依赖** Langfuse，停掉它业务照常跑，只是没有观测数据。

## 二、部署步骤

```bash
# 1. 生成环境变量文件（四项密钥无默认值，缺失时 Compose 直接报错）
cp deploy/langfuse/.env.langfuse.example deploy/langfuse/.env

# 2. 填密钥。ENCRYPTION_KEY 必须用下面这条命令生成，且**升级前必须备份**：
#    它加密库中的第三方凭据，更换后已存字段全部无法解密。
openssl rand -base64 32      # → LANGFUSE_ENCRYPTION_KEY
openssl rand -base64 48      # → LANGFUSE_SALT / LANGFUSE_NEXTAUTH_SECRET
# 依赖组件口令同样用 openssl rand 生成，不得留空或沿用示例值。

# 3. 启动（本文件可独立于业务 Compose 起停）
docker compose -f deploy/langfuse/docker-compose.langfuse.yml \
  --env-file deploy/langfuse/.env up -d

# 4. 查看状态；langfuse-web 日志出现 Ready 即就绪
docker compose -f deploy/langfuse/docker-compose.langfuse.yml ps
docker compose -f deploy/langfuse/docker-compose.langfuse.yml logs langfuse-web
```

**端口校验**：`docker compose -f deploy/langfuse/docker-compose.langfuse.yml port langfuse-web 3000`
应**无输出**，证明界面未发布到宿主。要访问界面，用 SSH 隧道（`ssh -L 3000:localhost:3000`）
或由 T6-10 的 Caddy 在 Compose 网络内反代；本机调试若确需临时映射端口，按第二节末的注意事项
只在本地改，不要提交。

### 首次进入界面后

在 Langfuse 界面创建组织与项目，把生成的 **public key / secret key** 填入业务侧的
`backend/.env`（见第五节），业务才会开始上报。

## 三、资源与卷

给 `langfuse-web` 与 `clickhouse` 设内存上限、给数据库与对象存储挂命名卷，是单机 Compose
阶段最实际的冲突点：Langfuse 与业务共享同一台机，无上限的观测后端会与业务抢内存。

| 命名卷 | 内容 | 说明 |
| --- | --- | --- |
| `huanong-langfuse_langfuse-postgres-data` | Langfuse 元数据（组织/项目/用户/密钥） | 体量小，但丢失需重建全部配置 |
| `huanong-langfuse_langfuse-clickhouse-data` | 观测事件（trace/observation） | **体量最大**，可设保留期定期清理 |
| `huanong-langfuse_langfuse-redis-data` | 队列与缓存 | 可重建，但丢队列会丢未落盘事件 |
| `huanong-langfuse_langfuse-minio-data` | 事件与多模态大对象 | 与 ClickHouse 一同构成观测数据 |

卷名前缀来自 Compose 项目的 `name: huanong-langfuse`。

## 四、升级

```bash
docker compose -f deploy/langfuse/docker-compose.langfuse.yml \
  --env-file deploy/langfuse/.env up --pull always -d
```

拉新镜像、重建容器，**数据在卷里，升级不改数据**。升级不影响业务：业务对 Langfuse 的
依赖是单向且弱依赖，Langfuse 停掉或升级中，问诊照常返回。

升级前必须备份 `LANGFUSE_ENCRYPTION_KEY` 与 `LANGFUSE_SALT`：前者换了会导致已存的加密
字段无法解密，后者换了会导致既有用户标识哈希对不上。

## 五、与业务的关系

业务侧需要的变量（写在 `backend/.env`，模板见 `backend/.env.example`）：

| 变量 | 说明 |
| --- | --- |
| `OTEL_ENABLED` | 打开追踪总开关 |
| `OTEL_EXPORTER` | 设为 `langfuse` 走上报私有的 Langfuse |
| `LANGFUSE_HOST` | Langfuse 地址。同一 Compose 网络内填 `http://langfuse-web:3000` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 界面创建的密钥 |
| `LANGFUSE_TRACE_RATIO` | 抽样比。本地 1.0 全量；**生产建议 0.2** |

**业务服务不设 `depends_on: langfuse`**（T6-10 的 Compose 中如此写）：观测后端故障不得
拖垮发布。若业务与 Langfuse 在不同 Compose 文件里，需让业务服务加入同一 external 网络：

```yaml
networks:
  langfuse:
    external: true
    name: huanong-langfuse
```

## 六、网络策略小结

- Langfuse 的 web 端口用 `expose` **不发布**到宿主；界面只在内网可达。
- ClickHouse / PostgreSQL / Redis / MinIO **一律不发布端口**，只有 Langfuse 自身可连。
- 若需对运维人员开放界面，用 Caddy 内网反代并叠加访问控制，不要直接把 3000 端口映射出去。
