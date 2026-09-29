"""pytest 全局配置：真实 PostgreSQL 测试库 + 异步会话隔离。

SQLite 内存库在本项目已不可用：`Vector` 列、`TIMESTAMPTZ` 默认值与
`create_hypertable` 都不是 SQLite 能力范围内的对象。测试库连接串由
`TEST_DATABASE_URL` 覆盖，默认指向本地 `huanong_test`。

建库走 `alembic upgrade head`（T1-04）：只有真实迁移链路才能暴露
"迁移漏建索引/漏加外键"这类问题，`Base.metadata.create_all` 会把这些掩盖掉。
"""

import asyncio
import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://huanong:huanong@localhost:5432/huanong_test"
)

# 必须在导入 app 之前覆盖配置，避免连到开发库
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# 检查点走 psycopg 的裸 postgresql 前缀，与 DATABASE_URL 的 driver 不同
os.environ["CHECKPOINT_DB_URL"] = TEST_DATABASE_URL.replace("+asyncpg", "")
os.environ.setdefault("USE_MOCK_LLM", "true")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("JWT_SECRET", "pytest-only-secret-key-at-least-32-bytes")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 清空顺序：子表在前，避免外键阻塞；process_redline 与 process_config 同版本，
# 排在其前以保持"被引用者在前"的一致顺序（两者无外键，顺序仅为可读性）。
_TABLES = (
    "event_outbox",
    "gate_audit",
    "gate_review_ticket",
    "process_redline",
    "process_config",
    "ai_message",
    "ai_conversation",
    "memory_summary",
    "user_profile_tag",
    "broadcast_log",
    "location",
    "device",
    "field",
    "crop",
    "farmer_profile",
    "user",
)

# 非 public schema 下的表：TRUNCATE 必须带 schema 前缀，否则按 search_path 找不到。
# knowledge_region 先于 knowledge_item、knowledge_item 先于 knowledge_review_log
# （子表/引用者在前，语义上被引用者靠后）。
_SCHEMA_TABLES = (
    ("knowledge", "knowledge_region"),
    ("knowledge", "knowledge_item"),
    ("knowledge", "knowledge_review_log"),
)


def _make_engine():
    """NullPool：测试进程内跨事件循环复用同一引擎不会串连接。"""
    return create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


def _alembic_config() -> Config:
    """构造指向测试库的 Alembic 配置（env.py 会再读 DATABASE_URL，已指向测试库）。"""
    cfg = Config(os.path.join(BACKEND_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_ROOT, "alembic"))
    return cfg


def postgres_reachable() -> bool:
    """探测测试库是否可用，供需要真实 PG 的用例做前置判断。"""

    async def _probe() -> bool:
        eng = _make_engine()
        try:
            async with eng.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001  探测失败原因不重要，一律视为不可用
            return False
        finally:
            await eng.dispose()

    try:
        return asyncio.run(_probe())
    except Exception:  # noqa: BLE001  连接串本身非法也视为不可用
        return False


@pytest.fixture(scope="session")
def requires_postgres() -> None:
    """需要真实 PostgreSQL 的用例统一用它做前置：不可用时跳过而非失败。"""
    if not postgres_reachable():
        pytest.skip(f"测试库不可用，跳过需要 PostgreSQL 的用例：{TEST_DATABASE_URL}")


@pytest.fixture(scope="session")
def migrated_db(requires_postgres):
    """会话级：用迁移把测试库建到 head，整轮结束后回退到 base。"""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield cfg
    command.downgrade(cfg, "base")


@pytest.fixture(scope="session")
def engine(migrated_db):
    """会话级引擎：库表由迁移建出，整轮测试共用。"""
    eng = _make_engine()
    yield eng
    asyncio.run(eng.dispose())


@pytest.fixture(autouse=True)
def clean_tables(request):
    """用例之间清空业务表，保证互不污染。

    只在用例真的用到数据库（声明了 `engine`，或声明了依赖它的
    `client` / `session_factory`）时才清表：否则一个 autouse fixture 会把
    "需要 PostgreSQL" 强加到纯静态用例上，让它们被整体跳过。
    """
    if "engine" not in request.fixturenames:
        yield
        return

    eng = request.getfixturevalue("engine")

    async def _truncate() -> None:
        async with eng.begin() as conn:
            for table in _TABLES:
                await conn.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))
            for schema, table in _SCHEMA_TABLES:
                await conn.execute(text(f'TRUNCATE TABLE "{schema}"."{table}" RESTART IDENTITY CASCADE'))

    asyncio.run(_truncate())
    yield


@pytest.fixture()
def session_factory(engine):
    """异步会话工厂，供仓储级用例自行开事务。"""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def run_async():
    """把协程放到独立事件循环执行的便捷入口，供同步用例做库断言。"""
    return asyncio.run


@pytest.fixture()
def client(engine):
    """TestClient 作为上下文管理器使用：整个 fixture 共享一个事件循环。

    这一点是必须的：Starlette 的 TestClient 若只做 `TestClient(app)` 而不进入
    上下文管理器，每次请求都会新建一个 anyio portal（即新事件循环）。而
    `app.db.session.engine` 是模块级连接池，连接一旦建立在循环 A，被循环 B
    复用时就会抛 `AttributeError: 'NoneType' object has no attribute 'send'`。
    进入上下文管理器后 lifespan 也一并执行，检查点连接池与业务引擎同循环，
    收尾时由 lifespan 统一关闭。
    """
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture()
def auth_token(client) -> str:
    """登录并返回已认证的 token。"""
    resp = client.post("/api/login", json={"code": "test-code-001"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    return data["token"]


@pytest.fixture()
def auth_headers(auth_token) -> dict:
    return {"Authorization": f"Bearer {auth_token}"}
