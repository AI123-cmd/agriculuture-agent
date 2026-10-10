"""启动期校验：不安全配置与结构漂移都在进程启动时拦下。

两类检查放在同一模块是因为它们的触发时机一致（lifespan 首行），但判据来源不同：
`collect_violations` 只看配置项，`verify_*` 要看数据库的真实状态。

为什么在启动时拒绝而不是运行时降级：带不安全配置对外服务的窗口期，可能已经产生了
越权数据或不合规输出，事后无法回滚。配置错误是"人能改的"，连通性/结构错误是
"环境待恢复的"，两类分开报便于定位。
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings

# 每次新增迁移后由发布流程更新此值。它与 `alembic/HEAD` 双写是刻意的：
# 前者是代码断言值（服务启动时校验），后者是发布产物校验值（流水线校验），
# 二者同时存在才能挡住"迁移加了但忘了更新期望值"。
EXPECTED_REVISION = "0013_ledger_entry"

# 检查点 schema 下应由 AsyncPostgresSaver.setup() 建出的表数量。
# 这些表的结构跟随 LangGraph 版本演进，故不纳入迁移管理，只断言数量下限。
_EXPECTED_CHECKPOINT_TABLES = 4

# JWT_SECRET 的长度下限。HS256 的强度直接取决于密钥长度，短密钥可被离线暴力破解。
_MIN_JWT_SECRET_LEN = 32


@dataclass(frozen=True)
class Check:
    """一条违规项：`name` 用于日志里定位，`detail` 说明为什么不允许。"""

    name: str
    detail: str


def collect_violations(settings: Settings) -> list[Check]:
    """返回全部违规项，而非在第一条就中断——便于一次修完所有配置。

    生产环境配置错误往往成批出现（新建环境时），逐条修会反复重启。
    """
    violations: list[Check] = []
    if settings.ENV != "production":
        # 非生产只要求数据库可定位：mock LLM、shadow 闸门、空密钥在这里都是合法的
        if not settings.DATABASE_URL:
            violations.append(Check("database", "DATABASE_URL 未配置"))
        return violations

    if not settings.WECHAT_APP_ID or not settings.WECHAT_APP_SECRET:
        violations.append(Check("wechat", "生产环境缺少微信密钥，登录必须真实校验（9.2 第 3 条）"))
    if settings.USE_MOCK_LLM:
        violations.append(Check("llm", "生产环境不允许 USE_MOCK_LLM=true"))
    if settings.GATE_MODE == "shadow":
        violations.append(Check("gate", "生产环境 GATE_MODE 不允许为 shadow（技术方案 14.2）"))
    if not settings.REDIS_URL:
        violations.append(Check("redis", "生产环境 Redis 为核心依赖，REDIS_URL 必填"))
    if not settings.JWT_SECRET or len(settings.JWT_SECRET) < _MIN_JWT_SECRET_LEN:
        violations.append(Check("jwt", f"JWT_SECRET 缺失或短于 {_MIN_JWT_SECRET_LEN} 字符"))
    if settings.DEBUG:
        violations.append(Check("debug", "生产环境 DEBUG 必须为 false，否则接口文档对外暴露"))
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        violations.append(Check("db_driver", "生产环境 DATABASE_URL 必须使用 asyncpg 驱动"))
    # CORS 为空时中间件不放行任何跨域来源——对小程序无碍（不在浏览器同源模型内），
    # 但后台管理页会整体不可用，属于"必然出错"的配置，故与通配来源一并拒绝。
    if not settings.CORS_ORIGINS:
        violations.append(Check("cors", "生产环境 CORS_ORIGINS 不能为空（技术方案 15.3）"))
    if "*" in settings.CORS_ORIGINS:
        violations.append(Check("cors", "生产环境不允许通配来源 *，必须显式列域名"))
    return violations


async def run_startup_checks() -> None:
    """服务启动时调用；有违规项则抛异常终止进程。"""
    settings = get_settings()
    violations = collect_violations(settings)
    if violations:
        lines = "\n".join(f"  - [{c.name}] {c.detail}" for c in violations)
        raise RuntimeError(f"启动自检未通过（ENV={settings.ENV}）：\n{lines}")
    await _check_runtime_dependencies(settings)


async def _check_runtime_dependencies(settings: Settings) -> None:
    """连通性检查：数据库必须可连；配置了 Redis 就必须连得上。

    排在配置检查之后：配置错误是"人能改的"，连通性错误是"环境待恢复的"。
    """
    from app.db.session import engine

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    if settings.REDIS_URL:
        from app.db.redis_client import get_redis

        client = get_redis()
        if client is None:
            raise RuntimeError("REDIS_URL 已配置但客户端未建立，拒绝启动")
        await client.ping()


async def verify_schema_revision(engine: AsyncEngine) -> None:
    """表结构版本与代码期望不一致时拒绝启动。"""
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT version_num FROM public.alembic_version"))
        current = row.scalar_one_or_none()
    if current != EXPECTED_REVISION:
        raise RuntimeError(
            f"迁移版本不匹配：数据库为 {current}，代码期望 {EXPECTED_REVISION}，请先执行 alembic upgrade head"
        )


async def verify_checkpoint_schema(engine: AsyncEngine) -> None:
    """断言 `checkpoint` schema 下至少存在 4 张检查点表。

    schema 缺失会让 LangGraph 建表落到 public（search_path 被静默忽略），
    因此这里既查 schema 存在性，也查表数量。
    """
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'checkpoint' AND table_type = 'BASE TABLE'"
                )
            )
        ).scalar_one()
    if count < _EXPECTED_CHECKPOINT_TABLES:
        raise RuntimeError(
            f"checkpoint schema 下仅有 {count} 张表，期望至少 {_EXPECTED_CHECKPOINT_TABLES} 张；"
            "请确认 checkpoint schema 已建且检查点器 setup() 已执行"
        )


__all__ = [
    "Check",
    "EXPECTED_REVISION",
    "collect_violations",
    "run_startup_checks",
    "verify_checkpoint_schema",
    "verify_schema_revision",
]
