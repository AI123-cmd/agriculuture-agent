"""T1-09 验收：端到端注入异常后数据库无残留。

`tests/test_transaction.py` 断言的是 `unit_of_work` 自身的行为；这里断言的是**接口
链路**上的行为：`/api/ai/chat` 的 `_persist` 分两步（先写 `ai_conversation`，再写
`ai_message`），让第二步抛异常，两张表都必须回到请求前的行数。

只断言返回码是不够的：改前的实现里 `_persist` 自己 `commit()`，会话行已经落库、
消息行失败，接口同样会返回 500，但库里留下一条没有消息的空会话。所以这里一律比对
行数快照。
"""

import asyncio
from collections.abc import Sequence

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AIConversation, AIMessage
from app.repositories.message import ConversationScopedRepository

pytestmark = pytest.mark.usefixtures("requires_postgres")


@pytest.fixture()
def counts(engine):
    """返回"统计 `ai_conversation` / `ai_message` 行数"的同步函数，内部自开会话。

    每次调用都新开会话：当前请求的会话里可能留着未提交的待写对象，用它统计会让
    断言失去意义（看到的正是待回滚的那一行）。
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    def _counts() -> tuple[int, int]:
        async def _query() -> tuple[int, int]:
            async with factory() as db:
                conv = (await db.execute(select(func.count()).select_from(AIConversation))).scalar_one()
                msg = (await db.execute(select(func.count()).select_from(AIMessage))).scalar_one()
                return conv, msg

        return asyncio.run(_query())

    return _counts


@pytest.fixture()
def fail_message_write(monkeypatch):
    """把 `add_many`（`_persist` 的第二步）换成必失败的桩。

    桩的签名不全标注：它只会抛异常，标注不带来任何检查收益。
    """

    def _boom(
        self: ConversationScopedRepository,
        db: AsyncSession,
        user_id: str,
        conv_id: str,
        messages: Sequence[AIMessage],
    ) -> Sequence[AIMessage]:
        raise RuntimeError("注入异常：写消息失败")

    monkeypatch.setattr(ConversationScopedRepository, "add_many", _boom)


def test_chat_persists_normally_without_injection(client, auth_headers, counts) -> None:
    """对照组：未注入异常时两条记录都落库，证明下面的断言有区分度。"""
    before = counts()

    resp = client.post(
        "/api/ai/chat",
        json={"message": "水稻叶片发黄", "sessionId": "sess-persist-ok", "cropTag": "水稻"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    after = counts()
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 2, "一轮对话写用户与 AI 两条消息"


def test_chat_rolls_back_both_tables_on_injected_failure(client, auth_headers, counts, fail_message_write) -> None:
    """注入异常后 `ai_conversation` 与 `ai_message` 行数均为注入前的值。"""
    before = counts()

    resp = client.post(
        "/api/ai/chat",
        json={"message": "稻飞虱怎么防", "sessionId": "sess-rollback-fail", "cropTag": "水稻"},
        headers=auth_headers,
    )

    assert resp.status_code == 500
    assert resp.json()["code"] == 500
    assert counts() == before, "注入失败后不应留下孤立会话或半条消息"


def test_chat_rollback_leaves_no_orphan_conversation(client, auth_headers, counts, fail_message_write) -> None:
    """失败请求不得留下"有会话无消息"的残行——孤立会话正是改前的真实后果。"""
    assert counts() == (0, 0)

    resp = client.post(
        "/api/ai/chat",
        json={"message": "排水沟怎么修", "sessionId": "sess-rollback-orphan", "cropTag": None},
        headers=auth_headers,
    )

    assert resp.status_code == 500
    assert counts() == (0, 0)


def test_no_idle_in_transaction_after_chat(client, engine, auth_headers, counts) -> None:
    """一次 `/api/ai/chat` 结束后，业务库不应有 `idle in transaction` 的连接残留。

    连接带事务回到池里意味着事务边界没收敛：`get_db` 的兜底回滚若失效，后续请求
    可能沿用到未结束的事务，读到不该看到的数据。这里直接问 PG。
    """
    resp = client.post(
        "/api/ai/chat",
        json={"message": "玉米追肥时间", "sessionId": "sess-idle-check", "cropTag": "玉米"},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    async def _idle_count() -> int:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle in transaction'"
                )
            )
            return int(result.scalar_one())

    assert asyncio.run(_idle_count()) == 0
