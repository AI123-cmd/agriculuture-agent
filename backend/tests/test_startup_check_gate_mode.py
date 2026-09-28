"""T2-46：生产环境禁止闸门影子模式（技术方案 14.2）。

影子模式的作用是"让规则先跑起来积累数据"，只在灰度期合法。灰度用 `ENV=staging`，全量切
生产时必须把 `GATE_MODE` 改为 `enforce`，因此"生产 + shadow"必须在启动自检阶段被拦下。

`collect_violations` 是纯函数（输入 `Settings`，输出违规项列表），故这里全部用构造好的
`Settings` 实例断言，不必改真实环境变量——那会波及同轮其它用例。
"""

from app.core.config import Settings
from app.core.startup_check import collect_violations

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


def _names(settings: Settings) -> set[str]:
    return {c.name for c in collect_violations(settings)}


def test_production_with_shadow_gate_is_rejected() -> None:
    settings = Settings(**{**_PROD_OK, "GATE_MODE": "shadow"})
    assert "gate" in _names(settings)


def test_production_with_enforce_gate_is_allowed() -> None:
    settings = Settings(**{**_PROD_OK, "GATE_MODE": "enforce"})
    assert collect_violations(settings) == []


def test_staging_with_shadow_gate_is_allowed() -> None:
    settings = Settings(
        ENV="staging",
        DATABASE_URL="postgresql+asyncpg://huanong:huanong@localhost:5432/huanong",
        GATE_MODE="shadow",
    )
    assert collect_violations(settings) == []


def test_development_with_shadow_gate_is_allowed() -> None:
    settings = Settings(
        ENV="development",
        DATABASE_URL="postgresql+asyncpg://huanong:huanong@localhost:5432/huanong",
        GATE_MODE="shadow",
    )
    assert collect_violations(settings) == []
