"""Alembic 迁移环境：绑定应用配置与 ORM 元数据。"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  确保所有模型注册到 Base.metadata
from alembic import context
from app.core.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` 不能省：fileConfig 默认会把"已经存在的 logger"
    # 全部置为 disabled。测试与脚本里 alembic 与应用同进程，默认行为会让 `app.*`
    # 的日志从此静默（连 pytest 的 caplog 也抓不到），告警与审计形同虚设。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 用应用配置覆盖连接串（从 .env / 环境变量读取）。
# 应用侧的 DATABASE_URL 是 asyncpg 驱动，而 Alembic 走同步 engine，需换成 psycopg。
_sync_url = get_settings().DATABASE_URL.replace("+asyncpg", "+psycopg")
config.set_main_option("sqlalchemy.url", _sync_url)

target_metadata = Base.metadata

# 只有这两个 schema 下的表由 ORM 元数据管辖：
# - public：11 张业务表（基线迁移）
# - knowledge：知识条目
# checkpoint schema 的表由 LangGraph 的 AsyncPostgresSaver.setup() 自建，
# 不纳入比对，否则 autogenerate 会不断生成"删掉检查点表"的错误差异；
# _timescaledb_* 是扩展的内部 schema，同理必须排除。
_COMPARED_SCHEMAS = {"knowledge"}

# 由原始 DDL 管理、ORM 元数据无法表达的表：
# - env_reading：TimescaleDB 超表，建表后还要 create_hypertable 改造，
#   autogenerate 只能看到一张普通表，比对必然失真，故排除。
# - checkpoint_*：LangGraph 自建，结构随其版本演进。
_DDL_MANAGED_TABLES = {"env_reading"}


def _is_ddl_managed(name: str | None) -> bool:
    return name in _DDL_MANAGED_TABLES or (name or "").startswith("checkpoint")


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """限制 autogenerate 的比对范围，避免把扩展与检查点表当成"待删除"。

    注意 `name is None` 必须放行：alembic 在调用名称过滤器之前会把"默认
    schema"（即 public）替换成 `None`，若一并拦掉，整张 public 会被判定为
    "库里不存在"，于是生成一堆 `create_table` 假差异。
    """
    if type_ == "schema":
        return name is None or name in _COMPARED_SCHEMAS
    if type_ == "table":
        return not _is_ddl_managed(name)
    return True


def include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """排除由原始 DDL 管理的表，其余一律参与比对。"""
    if type_ == "table" and _is_ddl_managed(name):
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        version_table_schema="public",
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 类型变更与默认值变更都要能被检测出来（技术方案 12.8）
            compare_type=True,
            compare_server_default=True,
            # 覆盖 knowledge 等非 public schema，否则这些表不参与比对
            include_schemas=True,
            # 版本表固定在 public，不随 search_path 漂移
            version_table_schema="public",
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
