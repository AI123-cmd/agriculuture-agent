# 后端镜像（T6-10，技术方案 15.1）。
#
# 多阶段构建：builder 装依赖并编译，runtime 只带运行期产物。这样镜像里没有编译工具链
# （gcc/pip/构建缓存），既减小体积也缩小攻击面——生产镜像不该带"能改自己"的工具。
#
# **非 root 运行**：容器默认可写根文件系统+running as root 是横向移动的便利条件；
# 这里建一个无家目录、不可登录的 `app` 用户，只给 `/app` 与静态目录写权限。
#
# **含 alembic**：迁移只在 api 启动时执行（见 deploy/api/entrypoint.sh），故运行镜像
# 必须带 `alembic` 与 `alembic/` 目录。迁移不靠 `create_all`（16.2 的硬要求）。

# --------------------------------------------------------------------------- builder
FROM python:3.11-slim AS builder

# 依赖编译需要 gcc（asyncpg/psycopg 的 C 扩展、cryptography 的 Rust 扩展）。
# 只装在 builder 阶段：runtime 阶段不建它们，最终镜像里没有编译器。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 依赖清单单独 COPY 并先装：改代码不会让这一层缓存失效，重建只重跑上层。
COPY requirements.txt ./
# `--prefix` 把依赖装到独立前缀，便于整目录复制到 runtime，无需再跑 pip。
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --------------------------------------------------------------------------- runtime
FROM python:3.11-slim AS runtime

# libpq5 是 psycopg/asyncpg 的运行期共享库（builder 装的 libpq-dev 是开发包，不进这里）。
# postgresql-client 提供 `pg_isready`（entrypoint 等数据库）与 `psql`（容器内排查），
# 是 api 入口脚本的显式依赖，不能省。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# 无 root 运行。`--no-create-home` + `/sbin/nologin` 让它不可登录、无家目录。
RUN useradd --create-home --shell /sbin/nologin app

WORKDIR /app

# 依赖来自 builder 的安装前缀；其 bin 在 PATH 里，故 `alembic` / `uvicorn` 可直接调用。
COPY --from=builder /install /usr/local

# 应用代码与迁移脚本。alembic/ 与 alembic.ini 必须进镜像：迁移在容器内执行。
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY pyproject.toml ./pyproject.toml

# 入口脚本来自 deploy/（构建上下文是 backend/，deploy 通过 Compose 的 build context 之外的
# 卷挂载不方便，故由 Compose 把 deploy/api 挂进容器；此处不 COPY，保持镜像只含后端自身）。

# 静态上传目录：本地对象存储会往这里写，须对非 root 用户可写。
RUN mkdir -p /app/uploads && chown -R app:app /app

USER app

# 默认命令留给 Compose 覆盖（api 用 entrypoint.sh，worker 用 arq）。这里给一个安全的默认：
# 直接起 uvicorn 单进程，便于 `docker run` 手工调试。
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
