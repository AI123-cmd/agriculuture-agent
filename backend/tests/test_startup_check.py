"""T1-08 验收：启动自检逐条行为。

`collect_violations` 是纯函数（输入 Settings，输出违规项列表），因此这里全部用构造好的
`Settings` 实例做断言，不需要真实环境变量或数据库——这样每条判据都能独立核对，也避免
用 `monkeypatch.setenv` 把测试进程自身的配置搅乱（那会波及同轮其它用例）。

连通性检查（`_check_runtime_dependencies`）依赖真实数据库，单独用一个用例覆盖。
"""

import pytest

from app.core.config import Settings
from app.core.startup_check import Check, collect_violations, run_startup_checks
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("requires_postgres")


def _load_preflight(alias: str):
    """把 `scripts/preflight.py` 当模块加载。它是脚本而非包内模块，只能按路径加载。"""
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"
    spec = importlib.util.spec_from_file_location(alias, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# 一份"生产环境全部合规"的基线，各用例只改需要破坏的那一项
_PROD_OK = {
    "ENV": "production",
    "DEBUG": False,
    "DATABASE_URL": "postgresql+asyncpg://huanong:huanong@localhost:5432/huanong",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "production-secret-key-at-least-32-bytes-long",
    "GATE_MODE": "enforce",
    "USE_MOCK_LLM": False,
    "WECHAT_APP_ID": "wx-production-appid",
    "WECHAT_APP_SECRET": "production-app-secret",
    "CORS_ORIGINS": ["https://admin.example.com"],
}


def _prod(**overrides) -> Settings:
    """生产环境配置，`overrides` 用于破坏其中某一项。"""
    return Settings(**{**_PROD_OK, **overrides})


def _names(violations: list[Check]) -> set[str]:
    return {c.name for c in violations}


def test_compliant_production_has_no_violations() -> None:
    """全部合规的生产配置必须零违规，否则自检会误伤正常发布。"""
    assert collect_violations(_prod()) == []


def test_production_without_wechat_secret_is_rejected() -> None:
    """生产缺微信密钥：报 `wechat`，对应 9.2 第 3 条。"""
    violations = collect_violations(_prod(WECHAT_APP_SECRET=""))
    assert "wechat" in _names(violations)


def test_production_with_mock_llm_is_rejected() -> None:
    """生产不允许 mock 模型：报 `llm`。"""
    assert "llm" in _names(collect_violations(_prod(USE_MOCK_LLM=True)))


def test_production_with_shadow_gate_is_rejected() -> None:
    """生产不允许闸门影子模式：报 `gate`（技术方案 14.2）。"""
    assert "gate" in _names(collect_violations(_prod(GATE_MODE="shadow")))


def test_production_with_debug_is_rejected() -> None:
    """生产不允许 DEBUG=true：报 `debug`，否则接口文档对外暴露。"""
    assert "debug" in _names(collect_violations(_prod(DEBUG=True)))


def test_production_with_non_asyncpg_driver_is_rejected() -> None:
    """生产 DATABASE_URL 必须是 asyncpg：报 `db_driver`。"""
    violations = collect_violations(_prod(DATABASE_URL="postgresql://huanong:huanong@localhost:5432/huanong"))
    assert "db_driver" in _names(violations)


def test_production_without_redis_is_rejected() -> None:
    """生产环境 Redis 为核心依赖：报 `redis`。"""
    assert "redis" in _names(collect_violations(_prod(REDIS_URL="")))


def test_production_with_short_jwt_secret_is_rejected() -> None:
    """JWT_SECRET 短于 32 字符：报 `jwt`。"""
    assert "jwt" in _names(collect_violations(_prod(JWT_SECRET="too-short")))


def test_production_without_cors_origins_is_rejected() -> None:
    """T1-10：生产 CORS_ORIGINS 为空即报 `cors`，否则后台管理页整体不可用。"""
    assert "cors" in _names(collect_violations(_prod(CORS_ORIGINS=[])))


def test_production_with_wildcard_cors_is_rejected() -> None:
    """T1-10：通配来源不能出现在生产白名单里。"""
    assert "cors" in _names(collect_violations(_prod(CORS_ORIGINS=["*"])))


def test_production_with_wildcard_among_origins_is_rejected() -> None:
    """混在具体域名里的 `*` 同样违规：漏配一项不会因为"还有别的域名"而放行。"""
    violations = collect_violations(_prod(CORS_ORIGINS=["https://admin.example.com", "*"]))
    assert "cors" in _names(violations)


def test_staging_without_cors_origins_is_allowed() -> None:
    """非生产不校验 CORS：本地调试页常用 localhost，白名单为空是常态。"""
    staging = Settings(
        ENV="staging",
        DATABASE_URL="postgresql+asyncpg://huanong:huanong@localhost:5432/huanong",
        CORS_ORIGINS=[],
    )
    assert collect_violations(staging) == []


def test_production_reports_all_violations_at_once() -> None:
    """同时缺密钥且开着 mock 时，两类违规一次全部报出，而不是只报第一条。

    这是"一轮修完所有配置"的前提：若 fail-fast，管理员要重启多次才能修完。
    """
    violations = collect_violations(_prod(WECHAT_APP_ID="", WECHAT_APP_SECRET="", USE_MOCK_LLM=True))
    names = _names(violations)
    assert {"wechat", "llm"} <= names


def test_staging_with_shadow_and_mock_is_allowed() -> None:
    """灰度期 staging + shadow + mock 合法：切生产前才需要收紧。"""
    staging = Settings(
        ENV="staging",
        DATABASE_URL="postgresql+asyncpg://huanong:huanong@localhost:5432/huanong",
        GATE_MODE="shadow",
        USE_MOCK_LLM=True,
        WECHAT_APP_ID="",
        WECHAT_APP_SECRET="",
        DEBUG=True,
    )
    assert collect_violations(staging) == []


def test_development_without_wechat_secret_is_allowed() -> None:
    """开发环境缺密钥与 mock 都合法：不牺牲本地开发效率。"""
    dev = Settings(
        ENV="development",
        DATABASE_URL="postgresql+asyncpg://huanong:huanong@localhost:5432/huanong",
        GATE_MODE="shadow",
        USE_MOCK_LLM=True,
        WECHAT_APP_ID="",
        WECHAT_APP_SECRET="",
    )
    assert collect_violations(dev) == []


def test_non_production_without_database_url_is_rejected() -> None:
    """非生产也必须能定位数据库：DATABASE_URL 为空即报 `database`。"""
    assert "database" in _names(collect_violations(Settings(ENV="development", DATABASE_URL="")))


def test_violation_message_lists_name_and_detail() -> None:
    """违规信息要能直接定位：格式为 `[名字] 说明`。"""
    violations = collect_violations(_prod(WECHAT_APP_SECRET=""))
    rendered = "\n".join(f"  - [{c.name}] {c.detail}" for c in violations)
    assert "[wechat]" in rendered
    assert len(violations[0].detail) > 0


async def test_run_startup_checks_passes_in_current_env(monkeypatch, engine) -> None:
    """当前测试环境（development + 可用数据库）必须通过自检。

    这条同时是连通性检查的覆盖：数据库连不上会在这里失败。

    必须把 `app.db.session.engine` 换成测试引擎（NullPool）：自检会真实建立连接，
    若用模块级全局引擎（QueuePool），连接会留在本用例的事件循环里，后续用例的
    `TestClient` 在另一个循环取到它就会抛 "attached to a different loop"。
    """
    monkeypatch.setattr("app.db.session.engine", engine)
    await run_startup_checks()


async def test_run_startup_checks_raises_on_violation(monkeypatch) -> None:
    """有违规项时抛 `RuntimeError`，消息里带违规项名，进程据此非 0 退出。"""
    monkeypatch.setattr("app.core.startup_check.get_settings", lambda: _prod(WECHAT_APP_SECRET="", USE_MOCK_LLM=True))

    with pytest.raises(RuntimeError) as exc:
        await run_startup_checks()

    message = str(exc.value)
    assert "[wechat]" in message
    assert "[llm]" in message


def test_docs_disabled_when_debug_false(monkeypatch) -> None:
    """`DEBUG=false` 时 `/docs`、`/redoc`、`/openapi.json` 均不注册（生产返回 404）。

    自检的 `debug` 检查与这里的路由关闭是双保险：前者拦截"忘了关 DEBUG"，
    后者保证即使 DEBUG 被人为置位，只要自检通过就不会暴露文档。
    """
    import importlib

    monkeypatch.setenv("DEBUG", "false")
    from app.core.config import get_settings as _get_settings

    _get_settings.cache_clear()

    import app.main as main_module

    reloaded = importlib.reload(main_module)
    assert reloaded.app.docs_url is None
    assert reloaded.app.redoc_url is None
    assert reloaded.app.openapi_url is None

    importlib.reload(main_module)
    _get_settings.cache_clear()


def test_docs_enabled_when_debug_true(monkeypatch) -> None:
    """开发环境 `DEBUG=true` 时文档可用，本地调试不受影响。"""
    import importlib

    monkeypatch.setenv("DEBUG", "true")
    from app.core.config import get_settings as _get_settings

    _get_settings.cache_clear()

    import app.main as main_module

    reloaded = importlib.reload(main_module)
    assert reloaded.app.docs_url == "/docs"

    importlib.reload(main_module)
    _get_settings.cache_clear()


def test_preflight_returns_nonzero_when_violations(monkeypatch) -> None:
    """`scripts/preflight.py` 的 main 在有违规项时返回 1，且违规项打到 stderr。"""
    module = _load_preflight("_preflight_probe")
    monkeypatch.setattr("app.core.startup_check.get_settings", lambda: _prod(JWT_SECRET=""))
    assert module.main() == 1


def test_preflight_returns_zero_on_compliant_config(monkeypatch, engine) -> None:
    """合规环境下返回 0。数据库指到测试库，避免依赖开发库是否存在。"""
    module = _load_preflight("_preflight_ok")
    monkeypatch.setattr(
        "app.core.startup_check.get_settings",
        lambda: Settings(ENV="development", DATABASE_URL=TEST_DATABASE_URL, REDIS_URL=""),
    )
    # `_check_runtime_dependencies` 在调用时才 `from app.db.session import engine`，
    # 因此替换模块属性即可生效。测试引擎用 NullPool，跨事件循环取用是安全的。
    monkeypatch.setattr("app.db.session.engine", engine)
    assert module.main() == 0
