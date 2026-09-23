"""T1-07 验收：令牌吊销与登出失效。

用真实 `TestClient`（进入上下文管理器以执行 lifespan）与真实登录 token，因为要
验证的正是"签发 → 使用 → 登出 → 同一个 token 立刻失效"这条端到端链路；用依赖覆盖
跳过 `get_current_user` 就把被测的校验逻辑整个绕开了。

`TokenRevoker` 的 Redis 分支用桩客户端覆盖（进程内名单与 Redis 名单是同一套语义，
差异只在存储位置），这样验收项"Redis 中 `EXISTS revoked:jti:<jti>` 为 1 且 TTL 不
超过 `TOKEN_EXPIRE_SECONDS`"无需一台真实 Redis 即可核对键名与 TTL 取值。
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_token, decode_token, remaining_ttl
from app.core.token_revoker import token_revoker

pytestmark = pytest.mark.usefixtures("requires_postgres")

_CODE = "revocation-user-0001"


@pytest.fixture(autouse=True)
def _clean_revoker():
    """吊销名单是进程级状态，必须逐用例清空，否则登出会污染后续用例。"""
    token_revoker.reset_memory()
    yield
    token_revoker.reset_memory()


def _login(client, code: str = _CODE) -> str:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200
    return resp.json()["data"]["token"]


def test_logout_revokes_token_immediately(client) -> None:
    """登出后同一 token 立即 401；重新登录签发的新 token 仍可用。

    最后一句是本用例的关键：它区分"按 jti 吊销"与"按 user_id 吊销"。若实现成后者，
    用户一登出就连新签发的令牌也一并失效，等于把用户锁在门外。
    """
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/farmer/profile", headers=headers).status_code == 200

    resp = client.post("/api/logout", headers=headers)
    assert resp.status_code == 200

    after = client.get("/api/farmer/profile", headers=headers)
    assert after.status_code == 401
    assert after.json()["code"] == 401

    new_token = _login(client)
    assert new_token != token
    assert client.get("/api/farmer/profile", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_logout_without_token_is_unauthorized(client) -> None:
    """未登录调登出返回 401，而不是静默成功。"""
    resp = client.post("/api/logout")
    assert resp.status_code == 401
    assert resp.json()["code"] == 401


def test_tampered_token_returns_401_not_500(client) -> None:
    """篡改载荷后验签失败，必须是 401 而不是 500。"""
    token = _login(client, "revocation-user-tamper")
    header, payload, signature = token.split(".")
    # 翻转载荷中段的字符：签名随之失效，且不会因 base64 末位填充位而侥幸不变
    mid = len(payload) // 2
    flipped = "A" if payload[mid] != "A" else "B"
    tampered = f"{header}.{payload[:mid]}{flipped}{payload[mid + 1 :]}.{signature}"

    resp = client.get("/api/farmer/profile", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == 401


def test_expired_token_returns_401(client) -> None:
    """过期令牌也是 401，与篡改走同一响应形态。"""
    settings = get_settings()
    past = datetime.now(UTC) - timedelta(seconds=10)
    expired = jwt.encode(
        {
            "sub": "user-not-matter",
            "jti": "jti-expired",
            "iat": int((past - timedelta(seconds=60)).timestamp()),
            "exp": int(past.timestamp()),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    resp = client.get("/api/farmer/profile", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


def test_token_for_deleted_user_is_rejected(client) -> None:
    """用户已注销时，其未过期令牌仍须被拒——否则注销形同虚设。"""
    token = create_token("user-does-not-exist-anywhere")
    resp = client.get("/api/farmer/profile", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_jti_is_unique_across_1000_tokens() -> None:
    """连续签发 1000 个令牌，jti 去重后仍是 1000 个（否则吊销会误伤他人）。"""
    jtis = {decode_token(create_token("user-bulk"))["jti"] for _ in range(1000)}
    assert len(jtis) == 1000


def test_token_carries_sub_and_exp() -> None:
    """令牌带 sub 与 exp：前者定位用户，后者决定吊销名单 TTL。"""
    payload = decode_token(create_token("user-payload"))
    assert payload["sub"] == "user-payload"
    assert payload["exp"] > int(datetime.now(UTC).timestamp())
    assert 0 < remaining_ttl(create_token("user-payload")) <= get_settings().TOKEN_EXPIRE_SECONDS


class _FakeRedis:
    """记录写入的最小 Redis 桩：只需要复现 setex / exists 的语义。"""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int]] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = (value, ttl)

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


async def test_revoked_jti_written_to_redis_with_ttl(monkeypatch) -> None:
    """登出写入 `revoked:jti:<jti>`，TTL 不超过令牌有效期。"""
    fake = _FakeRedis()

    async def _usable_redis():
        return fake

    monkeypatch.setattr("app.core.token_revoker.usable_redis", _usable_redis)

    token = create_token("user-redis-revoked", jti="jti-redis-001")
    await token_revoker.revoke("jti-redis-001", ttl=remaining_ttl(token))

    key = "revoked:jti:jti-redis-001"
    assert key in fake.store
    assert fake.store[key][0] == "1"
    assert 0 < fake.store[key][1] <= get_settings().TOKEN_EXPIRE_SECONDS
    assert await token_revoker.is_revoked("jti-redis-001") is True


async def test_memory_fallback_expires_entries(monkeypatch) -> None:
    """无 Redis 时走进程内名单，且过期条目会被清理，字典不会无限增长。"""

    async def _no_redis():
        return None

    monkeypatch.setattr("app.core.token_revoker.usable_redis", _no_redis)

    await token_revoker.revoke("jti-alive", ttl=60)
    await token_revoker.revoke("jti-dead", ttl=1)
    assert await token_revoker.is_revoked("jti-alive") is True

    # 让 jti-dead 自然过期后，读取时既返回 False 也把它剔除
    from app.core import token_revoker as module

    module._memory["jti-dead"] = 0.0
    assert await token_revoker.is_revoked("jti-dead") is False
    assert "jti-dead" not in module._memory


def test_logout_path_registered_in_openapi() -> None:
    """`/api/logout` 必须出现在 OpenAPI 契约里（前端据此对接）。"""
    from app.main import app as fastapi_app

    paths = fastapi_app.openapi()["paths"]
    assert "/api/logout" in paths
    assert "post" in paths["/api/logout"]


def test_revoke_token_symbol_no_longer_exists() -> None:
    """旧的 `revoke_token`（只定义不调用）必须已被移除，避免两套吊销机制并存。"""
    from app.core import security

    assert not hasattr(security, "revoke_token")
