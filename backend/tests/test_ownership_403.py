"""T1-05 验收：归属异常在 HTTP 层映射为 403，而不是 500。

本项只建基座、不改调用点（T1-06 才把 `ai_service` 的查询换成仓储），因此现有
路由还没有一处会抛 `OwnershipError`。这里自带一个探针路由，复用 `app.main`
真实的 `AppException` handler 与真实的 `conversation_repo`，把"仓储抛异常 →
全局 handler → HTTP 403 → 响应体不泄露资源存在"这条链路钉死。

真实 token 的端到端断言（B 的 token 请求 A 的会话）落在 T1-06 的
`tests/ownership/test_cross_user_access.py`：那一步才有路由使用仓储。

与其它 HTTP 用例的差别：这里用 httpx 的 ASGITransport 在**当前事件循环**内
发请求，并把 `get_db` 覆盖为测试引擎的会话。若改用 Starlette 的 TestClient，
它自建 portal 事件循环，会和 `app.db.session` 的连接池串循环
（`RuntimeError: ... attached to a different loop`）。
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.core.exceptions import AppException
from app.db.session import get_db
from app.main import app as fastapi_app
from app.main import app_exception_handler, unhandled_exception_handler
from app.models import AIConversation, User
from app.repositories import conversation_repo

pytestmark = pytest.mark.usefixtures("requires_postgres")

# 他人的 user_id：ai_conversation.user_id 无外键，不必先建 user 行
_FOREIGN_USER = "user-foreign-owner"


def _probe_app(current_user: str, session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """探针应用：共用生产异常处理，只把"当前用户"与"数据库会话"换成测试的。"""
    probe = FastAPI()
    probe.add_exception_handler(AppException, app_exception_handler)
    probe.add_exception_handler(Exception, unhandled_exception_handler)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _current_user() -> str:
        return current_user

    probe.dependency_overrides[get_db] = _session
    probe.dependency_overrides[get_current_user] = _current_user

    @probe.get("/probe/conversation/{conv_id}")
    async def read_conversation(
        conv_id: str,
        db: AsyncSession = Depends(get_db),
        user_id: str = Depends(get_current_user),
    ) -> dict:
        conv = await conversation_repo.get_or_raise(db, user_id, conv_id)
        return {"id": conv.id}

    return probe


@pytest.fixture()
async def current_user(engine) -> str:
    """测试用户（"B"）：登录流程之外直接建行，token 的签发/校验另有用例覆盖。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        user = User(id="user-current", openid="openid-current", gender=0)
        db.add(user)
        await db.commit()
    return "user-current"


@pytest.fixture()
async def foreign_conversation(engine) -> str:
    """他人（"A"）名下的一条会话，返回其 id。"""
    conv_id = "sess-foreign-001"
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        db.add(AIConversation(id=conv_id, user_id=_FOREIGN_USER, title="他人会话"))
        await db.commit()
    return conv_id


def _client(current_user: str, engine) -> httpx.AsyncClient:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    app = _probe_app(current_user, factory)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://probe")


def test_app_exception_handler_registered_on_real_app() -> None:
    """真实应用必须注册了 AppException handler，T1-06 的调用点才能自动拿到 403。"""
    assert AppException in fastapi_app.exception_handlers


async def test_cross_user_access_returns_403(engine, current_user, foreign_conversation) -> None:
    """读他人会话：HTTP 403 且 body 的 code 为 403，不是 500。"""
    async with _client(current_user, engine) as http:
        resp = await http.get(f"/probe/conversation/{foreign_conversation}")

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == 403
    assert resp.status_code != 500 and body["code"] != 500


async def test_forbidden_body_does_not_leak_existence(engine, current_user, foreign_conversation) -> None:
    """他人会话与不存在的会话返回完全相同的响应，无法据此枚举他人 id。"""
    async with _client(current_user, engine) as http:
        foreign = await http.get(f"/probe/conversation/{foreign_conversation}")
        absent = await http.get("/probe/conversation/sess-does-not-exist-at-all")

    assert foreign.status_code == absent.status_code == 403
    assert foreign.json() == absent.json()


async def test_own_conversation_still_readable(engine, current_user) -> None:
    """同一探针路由读自己的会话返回 200，证明 403 来自归属判断而非路由本身。"""
    conv_id = "sess-own-001"
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        db.add(AIConversation(id=conv_id, user_id=current_user, title="自己的会话"))
        await db.commit()

    async with _client(current_user, engine) as http:
        resp = await http.get(f"/probe/conversation/{conv_id}")

    assert resp.status_code == 200
    assert resp.json()["id"] == conv_id


def test_ownership_error_code_is_403_not_500() -> None:
    """异常自带的 code 就是 HTTP 状态码，handler 无需改动即可复用。"""
    from app.core.ownership import MissingOwnerError, OwnershipError

    assert OwnershipError().code == 403
    assert MissingOwnerError().code == 403


def test_missing_owner_error_still_caught_as_app_exception() -> None:
    """MissingOwnerError 必须能被 AppException handler 捕获（否则会变 500）。"""
    from app.core.ownership import MissingOwnerError

    assert issubclass(MissingOwnerError, AppException)
