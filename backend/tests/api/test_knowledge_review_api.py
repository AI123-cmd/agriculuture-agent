"""T2-54 验收：知识条目核准接口的权限与入参校验。

用真实 `TestClient` 打真实路由，因为要证明的正是接口层行为：农户令牌返回 403、合法后台
请求返回 200 且状态真的变了、非法迁移返回 409。数据库断言用会话工厂直接读回，避免只信
响应体。

权限白名单从 `Settings.KNOWLEDGE_REVIEW_OPERATOR_IDS` 读取，用例通过环境变量把它设成
"刚登录的那个账号"。登录用 mock 链路（开发环境），每个 code 对应一个独立用户 id。
"""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.models import KnowledgeItem, KnowledgeReviewLog

pytestmark = pytest.mark.usefixtures("requires_postgres")

_REVIEW_URL = "/api/knowledge/review/K-1023"


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """`get_settings` 是 lru_cache 的；用例改环境变量后必须清缓存，收尾再清一次，
    避免把"某个用例设过的白名单"泄漏给后续用例。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(client, code: str) -> tuple[str, str]:
    """登录并返回 (token, userId)。"""
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _set_operators(monkeypatch, *user_ids: str) -> None:
    """把白名单设为给定账号并清 Settings 缓存，使下一次 `get_settings()` 生效。"""
    monkeypatch.setenv("KNOWLEDGE_REVIEW_OPERATOR_IDS", ",".join(user_ids))
    get_settings.cache_clear()


def _seed_item(engine, knowledge_id: str, status: str = "draft") -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> None:
        async with factory() as db:
            db.add(
                KnowledgeItem(
                    knowledge_id=knowledge_id,
                    process_id="rice",
                    kind="pest",
                    title="稻飞虱识别与防治",
                    content="稻飞虱聚集在稻丛基部吸食汁液。",
                    status=status,
                )
            )
            await db.commit()

    asyncio.run(_run())


def _read_item(engine, knowledge_id: str) -> tuple[str, str | None]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> tuple[str, str | None]:
        async with factory() as db:
            row = (
                await db.execute(select(KnowledgeItem).where(KnowledgeItem.knowledge_id == knowledge_id))
            ).scalar_one()
            return row.status, row.status_reason

    return asyncio.run(_run())


def _log_count(engine) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> int:
        async with factory() as db:
            return (await db.execute(select(func.count()).select_from(KnowledgeReviewLog))).scalar_one()

    return asyncio.run(_run())


# ---- 权限 ----


def test_farmer_token_gets_403(client, engine, monkeypatch) -> None:
    """农户令牌调用核准接口返回 403，且状态未变、无日志。"""
    _seed_item(engine, "K-1023", status="draft")
    _, operator_id = _login(client, "operator-0001")
    farmer_token, _ = _login(client, "farmer-0002")
    _set_operators(monkeypatch, operator_id)

    resp = client.post(_REVIEW_URL, json={"action": "approve"}, headers=_headers(farmer_token))

    assert resp.status_code == 403
    assert resp.json()["code"] == 403
    assert _read_item(engine, "K-1023")[0] == "draft"
    assert _log_count(engine) == 0


def test_unauthenticated_gets_401(client, engine) -> None:
    """未带令牌调用返回 401（身份未确认），区别于已认证的 403。"""
    _seed_item(engine, "K-1023", status="draft")

    resp = client.post(_REVIEW_URL, json={"action": "approve"})

    assert resp.status_code == 401
    assert _read_item(engine, "K-1023")[0] == "draft"


def test_operator_in_empty_whitelist_gets_403(client, engine, monkeypatch) -> None:
    """白名单为空时任何账号都被拒（fail-closed）——默认配置不得放行。"""
    _seed_item(engine, "K-1023", status="draft")
    token, _ = _login(client, "operator-0001")
    monkeypatch.setenv("KNOWLEDGE_REVIEW_OPERATOR_IDS", "")
    get_settings.cache_clear()

    resp = client.post(_REVIEW_URL, json={"action": "approve"}, headers=_headers(token))

    assert resp.status_code == 403
    assert _read_item(engine, "K-1023")[0] == "draft"


# ---- 合法请求 ----


def test_operator_approve_changes_status(client, engine, monkeypatch) -> None:
    """合法后台请求返回 200 且状态从 draft 变为 approved，并写一条日志。"""
    _seed_item(engine, "K-1023", status="draft")
    token, operator_id = _login(client, "operator-0001")
    _set_operators(monkeypatch, operator_id)

    resp = client.post(_REVIEW_URL, json={"action": "approve"}, headers=_headers(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"knowledgeId": "K-1023", "status": "approved"}
    assert _read_item(engine, "K-1023")[0] == "approved"
    assert _log_count(engine) == 1


def test_operator_suspend_writes_reason(client, engine, monkeypatch) -> None:
    """暂停接口把理由写入主表 `status_reason`。"""
    _seed_item(engine, "K-1023", status="approved")
    token, operator_id = _login(client, "operator-0001")
    _set_operators(monkeypatch, operator_id)

    resp = client.post(_REVIEW_URL, json={"action": "suspend", "reason": "抗药性暂停"}, headers=_headers(token))

    assert resp.status_code == 200, resp.text
    status, reason = _read_item(engine, "K-1023")
    assert status == "suspended"
    assert reason == "抗药性暂停"


# ---- 入参校验 ----


def test_illegal_transition_returns_409(client, engine, monkeypatch) -> None:
    """`draft` 直接 `suspend`：接口返回 409。"""
    _seed_item(engine, "K-1023", status="draft")
    token, operator_id = _login(client, "operator-0001")
    _set_operators(monkeypatch, operator_id)

    resp = client.post(_REVIEW_URL, json={"action": "suspend", "reason": "越级"}, headers=_headers(token))

    assert resp.status_code == 409
    assert resp.json()["code"] == 409
    assert _read_item(engine, "K-1023")[0] == "draft"


def test_missing_reason_returns_400(client, engine, monkeypatch) -> None:
    """`suspend` 缺理由：接口返回 400。"""
    _seed_item(engine, "K-1023", status="approved")
    token, operator_id = _login(client, "operator-0001")
    _set_operators(monkeypatch, operator_id)

    resp = client.post(_REVIEW_URL, json={"action": "suspend"}, headers=_headers(token))

    assert resp.status_code == 400
    assert _read_item(engine, "K-1023")[0] == "approved"


def test_unknown_knowledge_id_returns_404(client, engine, monkeypatch) -> None:
    """条目不存在：接口返回 404。"""
    token, operator_id = _login(client, "operator-0001")
    _set_operators(monkeypatch, operator_id)

    resp = client.post("/api/knowledge/review/K-404", json={"action": "approve"}, headers=_headers(token))

    assert resp.status_code == 404
