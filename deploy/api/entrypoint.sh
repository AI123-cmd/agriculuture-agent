#!/usr/bin/env bash
# api 容器入口（T6-10，技术方案 15.1 与 16.2）。
#
# 固定顺序，三步都不可省：
#   1. 等数据库就绪（pg_isready 循环）——compose 的 healthcheck 已等一次，但迁移需要
#      的是"接受连接"而非"端口打开"，且 postgres 首次初始化会临时重启一次；
#   2. `alembic upgrade head`——**迁移只在 api 启动时执行一次**。worker 不执行迁移，
#      否则多进程会并发跑同一份迁移。
#   3. 启动 uvicorn（4 workers）。
#
# **不使用 `--reload`**：生产配置为固定 workers 数（15.1 拓扑注释），热重载会把主进程
# 变成文件监视器、并诱使有人靠重启复活内存态（缓存/熔断），与本项目的无状态副本假设冲突。
# **不使用 Gunicorn**：4.2 提到"生产配 Gunicorn"是备选表述，本项按 15.1 的 uvicorn 实现
# （回报中说明该不一致）。
set -euo pipefail

# 数据库主机/端口从 DATABASE_URL 拆出，避免在编排里重复声明 POSTGRES_HOST。
# SQLAlchemy 的连接串形如 postgresql+asyncpg://user:pass@host:port/db，
# 用 python 解析比 sed 可靠（密码里可能含 @ 或 :）。
read -r DB_HOST DB_PORT DB_USER DB_NAME <<<"$(python - <<'PY'
import os
from urllib.parse import urlsplit

url = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+psycopg", "")
parts = urlsplit(url)
print(parts.hostname or "postgres", parts.port or 5432, parts.username or "", (parts.path or "/db").lstrip("/"))
PY
)"

echo "[entrypoint] 等待数据库 ${DB_HOST}:${DB_PORT} 就绪..."
for attempt in $(seq 1 60); do
  if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q; then
    echo "[entrypoint] 数据库已就绪（第 ${attempt} 次探测）"
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    echo "[entrypoint] 数据库在 60 次探测后仍未就绪，退出" >&2
    exit 1
  fi
  sleep 2
done

# 迁移必须在 uvicorn 之前：结构漂移时不应先起服务再退出（startup_check 也会因
# EXPECTED_REVISION 不匹配拒绝启动，但那时进程已占用了端口与连接）。
echo "[entrypoint] 执行 alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] 启动 uvicorn（4 workers，无 --reload）..."
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 4 \
  --proxy-headers \
  --forwarded-allow-ips '*'
