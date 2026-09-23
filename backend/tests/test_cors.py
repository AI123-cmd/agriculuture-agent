"""T1-10 验收：CORS 白名单与预检行为。

两段式断言，各有理由：

1. **配置断言**直接读真实应用 `app.user_middleware` 上的 `CORSMiddleware` 参数。
   任务文件的验收命令是 `curl ... -i` 看响应头，本质是"看实际装配了什么"；把方法/
   头列表在测试里再抄一遍会随代码漂移，断言就变成了测试自己。
2. **行为断言**用同一份参数装配一个探针应用。之所以不直接打真实应用：白名单在
   `add_middleware` 时（模块导入期）就已固化，测试期间改环境变量不会生效，而重载
   `app.main` 会连带重建 lifespan 与全局引擎，污染同轮其它用例的事件循环。
   探针应用没有 lifespan、不碰数据库，因此可以放心地换白名单反复验证。

真实应用上用默认空白名单断言"白名单外域名被拒"这一条，端到端路径由它覆盖。
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app as real_app

_ALLOWED = "https://admin.example.com"
_EVIL = "https://evil.example"
_PROBE_PATH = "/probe"


def _cors_options() -> dict:
    """取出真实应用上 CORS 中间件的实际参数。"""
    for middleware in real_app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return dict(middleware.kwargs)
    raise AssertionError("应用未装配 CORSMiddleware")


def _split(value: object, *, upper: bool) -> set[str]:
    """归一成集合，兼容配置里的列表与响应头里的逗号分隔字符串。

    `allow_methods` / `allow_headers` 在配置层是列表，但 `CORSMiddleware` 写
    响应头时用 `", ".join(...)` 压成一个字符串，不切开会把整串当成一个元素。
    """
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = [str(item).strip() for item in value]  # type: ignore[union-attr]
    return {item.upper() if upper else item.lower() for item in items if item}


def _method_names(value: object) -> set[str]:
    return _split(value, upper=True)


def _header_names(value: object) -> set[str]:
    return _split(value, upper=False)


@pytest.fixture()
def probe() -> Iterator[TestClient]:
    """探针应用：与真实应用同一份 CORS 参数，只把白名单换成已知域名。"""
    options = _cors_options()
    options["allow_origins"] = [_ALLOWED]

    probe_app = FastAPI()
    probe_app.add_middleware(CORSMiddleware, **options)

    @probe_app.get(_PROBE_PATH)
    async def _echo() -> dict:
        return {"ok": True}

    with TestClient(probe_app) as test_client:
        yield test_client


# ----------------------------------------------------------------- 一、装配本身


def test_origins_come_from_settings_not_wildcard() -> None:
    """白名单取自配置，不得写死通配 `*`（等价于验收里的 grep 命令）。"""
    assert _cors_options()["allow_origins"] == get_settings().CORS_ORIGINS
    assert "*" not in _cors_options()["allow_origins"]


def test_wildcard_literal_is_absent_from_source() -> None:
    """源码里不再出现 `allow_origins=["*"]` 这一写法。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert 'allow_origins=["*"]' not in source


def test_methods_are_explicit_without_wildcard() -> None:
    """方法显式列出，不含 `*`：新增方法却漏配会在预检阶段暴露。"""
    methods = _method_names(_cors_options()["allow_methods"])

    assert "*" not in methods
    assert {"GET", "POST", "PUT", "DELETE", "OPTIONS"} <= methods


def test_headers_are_explicit_and_include_authorization() -> None:
    """`Authorization` 必需（Bearer token），`X-Request-Id` 为链路追踪预留。"""
    headers = _header_names(_cors_options()["allow_headers"])

    assert "*" not in headers
    assert {"authorization", "content-type", "x-request-id"} <= headers


def test_credentials_allowed_only_with_explicit_origins() -> None:
    """带凭据必须配具体域名：`*` 与 `credentials=True` 不能共存（这正是改前的矛盾）。"""
    options = _cors_options()

    assert options["allow_credentials"] is True
    assert "*" not in options["allow_origins"]


def test_request_id_is_exposed() -> None:
    """`X-Request-Id` 对前端公开，否则浏览器读不到该响应头。"""
    exposed = _header_names(_cors_options()["expose_headers"])

    assert "x-request-id" in exposed


# ----------------------------------------------------------------- 二、真实应用


def test_origin_outside_allowlist_gets_no_cors_header(client) -> None:
    """默认白名单为空，任何跨域来源都拿不到 `Access-Control-Allow-Origin`。"""
    resp = client.get("/api/farmer/profile", headers={"Origin": _EVIL})

    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_same_origin_request_is_not_blocked(client) -> None:
    """不带 `Origin` 的同源请求正常进入业务逻辑（未带 token，故 401 而非被 CORS 拒）。"""
    resp = client.get("/api/farmer/profile")

    assert resp.status_code == 401
    assert resp.json()["code"] == 401


# ----------------------------------------------------------------- 三、白名单行为


def test_origin_inside_allowlist_is_reflected(probe: TestClient) -> None:
    """白名单内域名被回显，且带 `Allow-Credentials: true`。"""
    resp = probe.get(_PROBE_PATH, headers={"Origin": _ALLOWED})

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == _ALLOWED
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_origin_outside_allowlist_is_not_reflected(probe: TestClient) -> None:
    """同一次装配下，白名单外域名拿不到该头——证明回显来自白名单而不是无条件放行。"""
    resp = probe.get(_PROBE_PATH, headers={"Origin": _EVIL})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_preflight_returns_200_with_explicit_methods(probe: TestClient) -> None:
    """预检 200，`Allow-Methods` 是显式列表而非 `*`。"""
    resp = probe.options(
        _PROBE_PATH,
        headers={
            "Origin": _ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert resp.status_code == 200
    methods = _method_names(resp.headers["access-control-allow-methods"])
    assert "*" not in methods
    assert {"GET", "POST", "PUT", "DELETE", "OPTIONS"} <= methods


def test_preflight_does_not_advertise_patch(probe: TestClient) -> None:
    """本项目没有 `PATCH` 接口，预检不应宣告它。"""
    resp = probe.options(
        _PROBE_PATH,
        headers={"Origin": _ALLOWED, "Access-Control-Request-Method": "PATCH"},
    )

    assert "PATCH" not in _method_names(resp.headers.get("access-control-allow-methods", ""))


def test_preflight_allows_authorization_header(probe: TestClient) -> None:
    """`Authorization` 在 `Allow-Headers` 里，否则前端带 token 的跨域请求全挂。"""
    resp = probe.options(
        _PROBE_PATH,
        headers={
            "Origin": _ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    allowed = _header_names(resp.headers["access-control-allow-headers"])
    assert {"authorization", "content-type"} <= allowed


def test_preflight_result_is_cacheable(probe: TestClient) -> None:
    """预检结果可缓存：`Max-Age` 为正，减少重复 OPTIONS 请求。"""
    resp = probe.options(
        _PROBE_PATH,
        headers={"Origin": _ALLOWED, "Access-Control-Request-Method": "POST"},
    )

    assert int(resp.headers["access-control-max-age"]) > 0
