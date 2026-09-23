"""启动期结构校验：迁移版本与代码期望不一致时拒绝启动。

为什么要在启动时断言而不是等到第一次查询报错：表结构漂移（迁移没跑、跑了一半、
或跑了别的分支）在运行期表现为随机 500，排查成本极高；在启动期一次性拦下，
错误信息直接指向"执行 alembic upgrade head"。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# 每次新增迁移后由发布流程更新此值。它与 `alembic/HEAD` 双写是刻意的：
# 前者是代码断言值（服务启动时校验），后者是发布产物校验值（流水线校验），
# 二者同时存在才能挡住"迁移加了但忘了更新期望值"。
EXPECTED_REVISION = "0002_env_reading"

# 检查点 schema 下应由 AsyncPostgresSaver.setup() 建出的表数量。
# 这些表的结构跟随 LangGraph 版本演进，故不纳入迁移管理，只断言数量下限。
_EXPECTED_CHECKPOINT_TABLES = 4


async def verify_schema_revision(engine: AsyncEngine) -> None:
    """表结构版本与代码期望不一致时拒绝启动。"""
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT version_num FROM public.alembic_version"))
        current = row.scalar_one_or_none()
    if current != EXPECTED_REVISION:
        raise RuntimeError(
            f"迁移版本不匹配：数据库为 {current}，代码期望 {EXPECTED_REVISION}，"
            "请先执行 alembic upgrade head"
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
