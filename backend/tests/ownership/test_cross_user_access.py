"""T1-06 验收：跨用户访问矩阵（每类资源至少一条读断言 + 一条写断言）。

分两组机制，各有理由：

1. 大多数用例用 `httpx.ASGITransport` 在**当前事件循环**内直接打真实 `app.main`，
   只把 `get_db`（指向测试库）与 `get_current_user`（指定"我是谁"）换成测试版本。
   这些路由不触达 LangGraph 检查点，无需 lifespan，跑得快且不依赖检查点连接池。
2. `/api/ai/chat` 必须走真实 `TestClient`（进入上下文管理器以执行 lifespan）：
   `AIService.chat` 会 `get_agent()`，检查点连接池由 lifespan 建立并收尾。
   它也顺带用真实登录 token 而非依赖覆盖，覆盖了"真实凭证"这条路径。

写断言一律比对前后快照，而不是只看返回码：返回码正确但数据被改，正是本项要防的
真实情形（原 `ai_service._persist` 就是这种）。
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models import AIConversation, AIMessage, BroadcastLog, FarmerProfile, UserProfileTag
from tests.fixtures.two_users import TwoUsers, two_users  # noqa: F401  夹具需在本模块可见

pytestmark = pytest.mark.usefixtures("requires_postgres")


def _client(user_id: str, factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    """以 `user_id` 的身份访问真实应用（依赖覆盖，不走 token）。"""

    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def _current_user() -> str:
        return user_id

    fastapi_app.dependency_overrides[get_db] = _session
    fastapi_app.dependency_overrides[get_current_user] = _current_user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides() -> AsyncIterator[None]:
    """依赖覆盖是进程级全局状态，跑完必须清掉，否则污染同轮其它用例。"""
    yield
    fastapi_app.dependency_overrides.clear()


async def _count(factory: async_sessionmaker[AsyncSession], model, **conditions) -> int:
    async with factory() as db:
        stmt = select(func.count()).select_from(model)
        for field, value in conditions.items():
            stmt = stmt.where(getattr(model, field) == value)
        return (await db.execute(stmt)).scalar_one()


async def _scalar(factory: async_sessionmaker[AsyncSession], column, **conditions):
    async with factory() as db:
        stmt = select(column)
        for field, value in conditions.items():
            stmt = stmt.where(getattr(column.table.c, field) == value)
        return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------- 会话（读 + 写）


async def test_conversation_read_by_other_user_is_403(two_users: TwoUsers) -> None:
    """B 请求删 A 的会话：403，且 A 的会话与消息一行未少。"""
    factory = two_users.session_factory
    before_conv = await _count(factory, AIConversation)
    before_msg = await _count(factory, AIMessage)

    async with _client(two_users.b, factory) as http:
        resp = await http.delete(f"/api/ai/history/{two_users.a_conversation}")

    assert resp.status_code == 403
    assert resp.json()["code"] == 403
    assert await _count(factory, AIConversation) == before_conv
    assert await _count(factory, AIMessage) == before_msg


async def test_forbidden_conversation_body_matches_absent_one(two_users: TwoUsers) -> None:
    """他人会话与不存在会话的响应完全一致，无法据此枚举他人 sessionId。"""
    async with _client(two_users.b, two_users.session_factory) as http:
        foreign = await http.delete(f"/api/ai/history/{two_users.a_conversation}")
        absent = await http.delete("/api/ai/history/sess-not-exists-anywhere")

    assert foreign.status_code == absent.status_code == 403
    assert foreign.json() == absent.json()


async def test_own_conversation_is_deletable(two_users: TwoUsers) -> None:
    """对照：A 删自己的会话成功，证明 403 来自归属判断而非路由本身。"""
    async with _client(two_users.a, two_users.session_factory) as http:
        resp = await http.delete(f"/api/ai/history/{two_users.a_conversation}")

    assert resp.status_code == 200
    assert await _count(two_users.session_factory, AIMessage, conversation_id=two_users.a_conversation) == 0


# ---------------------------------------------------------------- 档案（读 + 写）


async def test_profile_read_returns_only_own(two_users: TwoUsers) -> None:
    """B 读档案拿到自己的那一条，看不到 A 的姓名与地区。"""
    async with _client(two_users.b, two_users.session_factory) as http:
        resp = await http.get("/api/farmer/profile")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["hasProfile"] is True
    assert data["profile"]["realName"] == "乙农户"
    assert "甲农户" not in resp.text


async def test_profile_write_does_not_overwrite_other_user(two_users: TwoUsers) -> None:
    """B 保存档案后，A 的档案内容不变、档案总行数不变。"""
    factory = two_users.session_factory
    before = await _count(factory, FarmerProfile)

    payload = {
        "realName": "乙改名",
        "age": 30,
        "gender": 1,
        "phone": "13900000000",
        "experienceYear": 3,
        "province": "湖南省",
        "city": "长沙市",
        "district": "岳麓区",
        "address": "YY村",
        "totalMu": 5.0,
        "cultivationType": 1,
        "waterSource": "井水",
        "remark": "改过的档案",
        "crops": [{"cropName": "烟草", "plantMu": 5.0, "growthStage": "旺长期"}],
    }
    async with _client(two_users.b, factory) as http:
        resp = await http.post("/api/farmer/profile", json=payload)

    assert resp.status_code == 200
    assert await _count(factory, FarmerProfile) == before

    async with factory() as db:
        a_profile = (await db.execute(select(FarmerProfile).where(FarmerProfile.user_id == two_users.a))).scalar_one()
        assert a_profile.real_name == "甲农户"
        assert a_profile.province == "江西省"

        b_profile = (await db.execute(select(FarmerProfile).where(FarmerProfile.user_id == two_users.b))).scalar_one()
        assert b_profile.real_name == "乙改名"


# ---------------------------------------------------------------- 标签（读 + 写）


async def test_profile_tags_write_is_scoped_to_owner(two_users: TwoUsers) -> None:
    """B 保存档案刷新的是 B 自己的标签，A 的标签行内容一字未动。"""
    factory = two_users.session_factory

    async with factory() as db:
        before_a = list(
            (await db.execute(select(UserProfileTag.tags).where(UserProfileTag.user_id == two_users.a))).scalar_one()
        )

    payload = {
        "realName": "乙农户",
        "crops": [{"cropName": "烟草", "plantMu": 1.0, "growthStage": "旺长期"}],
    }
    async with _client(two_users.b, factory) as http:
        resp = await http.post("/api/farmer/profile", json=payload)
    assert resp.status_code == 200

    async with factory() as db:
        after_a = list(
            (await db.execute(select(UserProfileTag.tags).where(UserProfileTag.user_id == two_users.a))).scalar_one()
        )
        after_b = list(
            (await db.execute(select(UserProfileTag.tags).where(UserProfileTag.user_id == two_users.b))).scalar_one()
        )

    assert before_a == after_a == ["甲的标签"]
    assert after_b == ["烟草"]


# ---------------------------------------------------------------- 设备（仅读）


async def test_environment_detail_excludes_other_users_devices(two_users: TwoUsers) -> None:
    """B 的环境详情里只有 B 的设备，A 的设备不可见。"""
    async with _client(two_users.b, two_users.session_factory) as http:
        resp = await http.get("/api/environment/detail")

    assert resp.status_code == 200
    ids = [d["id"] for d in resp.json()["data"]["devices"]]
    assert ids == [two_users.b_device]
    assert two_users.a_device not in ids


# ---------------------------------------------------------------- 播报（读 + 写）


async def test_broadcast_today_excludes_other_users_items(two_users: TwoUsers) -> None:
    """B 的今日播报不含 A 的条目（A 的条目确实存在，故该断言有区分度）。"""
    factory = two_users.session_factory
    assert await _count(factory, BroadcastLog, id=two_users.a_broadcast) == 1

    async with _client(two_users.b, factory) as http:
        resp = await http.get("/api/broadcast/today")

    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["data"]["items"]]
    assert two_users.a_broadcast not in ids
    assert two_users.b_broadcast in ids


async def test_mark_done_on_other_users_item_changes_nothing(two_users: TwoUsers) -> None:
    """B 标记 A 的播报完成：请求成功但 A 的 done 仍为 0。"""
    factory = two_users.session_factory
    async with _client(two_users.b, factory) as http:
        resp = await http.post("/api/broadcast/mark-done", json={"itemId": two_users.a_broadcast})

    assert resp.status_code == 200
    async with factory() as db:
        done = (
            await db.execute(select(BroadcastLog.done).where(BroadcastLog.id == two_users.a_broadcast))
        ).scalar_one()
    assert done == 0


async def test_mark_done_on_own_item_works(two_users: TwoUsers) -> None:
    """对照：B 标记自己的播报生效，证明上一条的 0 来自归属过滤。"""
    factory = two_users.session_factory
    async with _client(two_users.b, factory) as http:
        resp = await http.post("/api/broadcast/mark-done", json={"itemId": two_users.b_broadcast})

    assert resp.status_code == 200
    async with factory() as db:
        done = (
            await db.execute(select(BroadcastLog.done).where(BroadcastLog.id == two_users.b_broadcast))
        ).scalar_one()
    assert done == 1


# ------------------------------------------------- 会话接管（真实 token / 真实 lifespan）


def _login(client, code: str) -> tuple[str, dict]:
    """真实登录，返回 (userId, Authorization 头)。

    调用方给的两个 code 必须在前 8 个字符上就不同：本地 mock 的 openid 取
    `code[:8]`，前缀相同会被判成同一个微信用户，跨用户断言随即失去意义。
    """
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200
    data = resp.json()["data"]
    return data["userId"], {"Authorization": f"Bearer {data['token']}"}


# 前 8 字符必须互不相同，见 `_login` 的说明
_CODE_A = "cross-UA-0001"
_CODE_B = "cross-UB-0002"


def test_chat_with_foreign_session_id_does_not_take_over(client) -> None:
    """A 带着 B 的 sessionId 发消息：B 的会话与消息一行未变，A 名下多出新会话。

    这是第一类高危缺陷的正面回击：改前 A 会把消息写进 B 的会话，并改写其
    `crop_tag`；现在归属不符当作新会话处理，且新会话一定用新主键。

    用真实 `TestClient`（上下文管理器，跑 lifespan）与真实登录 token：
    `/api/ai/chat` 会 `get_agent()`，检查点连接池必须由 lifespan 建立与收尾。
    """
    a_id, a_headers = _login(client, _CODE_A)
    b_id, b_headers = _login(client, _CODE_B)
    assert a_id != b_id

    # B 先建一条自己的会话，并在其中留下可辨识的内容
    b_session = "sess-b-owned-001"
    resp = client.post(
        "/api/ai/chat",
        json={"message": "水稻叶片发黄", "sessionId": b_session},
        headers=b_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["sessionId"] == b_session

    a_before = client.get("/api/ai/history", headers=a_headers).json()["data"]["total"]
    b_before = client.get("/api/ai/history", headers=b_headers).json()["data"]

    # A 拿 B 的 sessionId 发消息
    resp = client.post(
        "/api/ai/chat",
        json={"message": "稻飞虱怎么防", "sessionId": b_session},
        headers=a_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["sessionId"] != b_session, "不得接管他人会话主键"

    # A 名下多出一条新会话，且看不到 B 的会话
    a_after = client.get("/api/ai/history", headers=a_headers).json()["data"]
    assert a_after["total"] == a_before + 1
    assert all(item["id"] != b_session for item in a_after["list"])

    # B 的会话条数、预览与标题都必须原样保留
    # （T1-11 后列表不再带 `messages`，改用 `messageCount` 与 `preview` 核对未被改动）
    b_after = client.get("/api/ai/history", headers=b_headers).json()["data"]
    assert b_after["total"] == b_before["total"]
    b_items_before = {item["id"]: item for item in b_before["list"]}
    b_items_after = {item["id"]: item for item in b_after["list"]}
    assert b_items_after[b_session]["title"] == b_items_before[b_session]["title"]
    assert b_items_after[b_session]["messageCount"] == b_items_before[b_session]["messageCount"]
    assert b_items_after[b_session]["preview"] == b_items_before[b_session]["preview"]
    assert all("稻飞虱" not in item["preview"] for item in b_after["list"])
