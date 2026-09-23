"""T1-07 验收：登录链路的环境隔离。

被测的行为是"生产环境缺微信密钥时不得降级为模拟登录"。用真实 `TestClient` 打真实
登录路由，因为要证明的正是"接口层面没有签发 token、也没有建用户"，而不是某一个函数
抛了异常；直接断言返回码与库中行数，比断言异常类型更贴近这条验收项的字面要求。

生产环境数据库连接的可用性由 T1-08 的启动自检负责，不在本用例范围内。
"""

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError
from app.models import User

pytestmark = pytest.mark.usefixtures("requires_postgres")


@pytest.fixture()
def prod_env(monkeypatch):
    """切到 production 且清空微信密钥，用完恢复配置缓存。

    `get_settings` 是 `lru_cache` 的，改环境变量后必须清缓存才会重新读取；
    不清缓存会让"设定 production 却仍按 development 执行"这类错误无法暴露。
    """
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("WECHAT_APP_ID", "")
    monkeypatch.setenv("WECHAT_APP_SECRET", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def dev_env(monkeypatch):
    """切到 development 且清空微信密钥（模拟登录应可用）。"""
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WECHAT_APP_ID", "")
    monkeypatch.setenv("WECHAT_APP_SECRET", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _user_count(engine) -> int:
    """同步读取用户行数：供同步用例做前后快照比对。"""
    import asyncio

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _count() -> int:
        async with factory() as db:
            return (await db.execute(select(func.count()).select_from(User))).scalar_one()

    return asyncio.run(_count())


def test_settings_reports_production(prod_env) -> None:
    """前置自检：环境变量确实生效，否则后续断言都是假的。"""
    settings = Settings()
    assert settings.ENV == "production"
    assert not settings.WECHAT_APP_SECRET


def test_production_without_wechat_secret_rejects_login(client, engine, prod_env) -> None:
    """生产环境缺密钥：登录返回 500 与 code=500，且一个用户都没建出来。"""
    before = _user_count(engine)

    resp = client.post("/api/login", json={"code": "prod-no-secret-code"})

    assert resp.status_code == 500
    assert resp.json()["code"] == 500
    assert "token" not in resp.text
    assert _user_count(engine) == before


async def test_production_login_raises_config_error(prod_env) -> None:
    """服务层同样直接抛 `ConfigError`，不返回任何 token。"""
    from app.services import get_auth_service

    with pytest.raises(ConfigError):
        await get_auth_service().login(None, "prod-no-secret-code")  # type: ignore[arg-type]


def test_development_without_wechat_secret_still_works(client, dev_env) -> None:
    """开发环境缺密钥：模拟登录可用，token 正常签发（不牺牲本地开发效率）。"""
    resp = client.post("/api/login", json={"code": "dev-mock-code-0001"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["token"]
    assert data["userId"]


def test_mock_login_openids_are_distinct_per_code(dev_env) -> None:
    """mock openid 必须逐 code 不同：否则跨用户测试会把两人当成同一人。"""
    from app.services import get_auth_service

    service = get_auth_service()
    first = service._mock_login("cross-user-a")["openid"]
    second = service._mock_login("cross-user-b")["openid"]

    assert first != second
    assert service._mock_login("cross-user-a")["openid"] == first


def test_code2session_raises_without_keys(dev_env) -> None:
    """集成层不再自行降级：缺密钥抛 `ConfigError`，策略判断留在服务层。"""
    import asyncio

    from app.integrations.wechat import code2session

    with pytest.raises(ConfigError):
        asyncio.run(code2session("any-code"))


def test_code2session_absent_when_keys_present(monkeypatch) -> None:
    """密钥齐全时走真实 HTTP。用 respx 拦截，失败（errcode）必须转成业务异常。"""
    import asyncio

    import httpx
    import respx

    from app.core.exceptions import AppException
    from app.integrations.wechat import code2session

    monkeypatch.setenv("WECHAT_APP_ID", "wx-test-appid")
    monkeypatch.setenv("WECHAT_APP_SECRET", "test-app-secret")
    get_settings.cache_clear()

    with respx.mock:
        respx.get("https://api.weixin.qq.com/sns/jscode2session").mock(
            return_value=httpx.Response(200, json={"openid": "real-openid-001", "session_key": "sk"})
        )
        data = asyncio.run(code2session("real-code"))
    assert data["openid"] == "real-openid-001"

    with respx.mock:
        respx.get("https://api.weixin.qq.com/sns/jscode2session").mock(
            return_value=httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})
        )
        with pytest.raises(AppException):
            asyncio.run(code2session("bad-code"))

    get_settings.cache_clear()


def test_env_must_be_one_of_three_known_values(monkeypatch) -> None:
    """`ENV` 是封闭枚举：`prod` 这类拼写必须在构造 Settings 时就被拒绝。"""
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(Exception):
        Settings()
    monkeypatch.setenv("ENV", "development")


def test_conftest_does_not_leak_production_env() -> None:
    """测试进程自身不得处于 production：否则正常用例会被启动链路拦住。"""
    assert os.environ.get("ENV") != "production"
