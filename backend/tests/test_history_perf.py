"""T1-11 验收：历史列表的查询数与执行计划。

四条断言各自对应一种风险，不能互相替代：

1. **查询数**（`before_cursor_execute` 事件计数）：钉死"不再逐条查消息"。
   改前 `size=20` 要 22 次查询，只看返回结果对不对是发现不了的——N+1 的返回
   结果与批量查询完全一致，只有数语句数才能证明它消失了。
2. **执行计划**：钉死"没有把 `ai_message` 全表扫一遍"。查询数对不代表扫描范围
   合理：窗口函数版同样只发 1 条 SQL，但那条 SQL 会把所有租户的消息都分区一遍。
3. **响应结构**：`preview` 必须是最后一条消息的前 30 字符、`messageCount` 必须
   等于真实消息数，且无消息的会话不能丢。
4. **截断**：`size=100` 必须在服务端截到上限，不能由前端决定拉多少。

查询数用"触碰 `ai_conversation` / `ai_message` 的语句"计数，而不是全部语句：
鉴权链路也会查库（`user` 表、吊销名单），把它算进来会让断言变成对登录实现的断言。
"""

import re
import time

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import engine as app_engine
from app.models import AIConversation
from app.repositories import conversation_repo
from tests.factories.history_fixtures import (
    CONVERSATION_COUNT,
    MESSAGES_PER_CONVERSATION,
    expected_preview,
    last_message_content,
    seed_history,
    seed_history_sync,
)

pytestmark = pytest.mark.usefixtures("requires_postgres")

_AI_TABLE = re.compile(r"\bai_(conversation|message)\b", re.IGNORECASE)
_CODE = "history-perf-0001"
_MAX_PAGE_SIZE = 50


class _QueryCounter:
    """挂在引擎上的语句计数器。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        self.statements.append(statement)

    @property
    def ai_statements(self) -> list[str]:
        return [s for s in self.statements if _AI_TABLE.search(s)]

    @property
    def message_statements(self) -> list[str]:
        return [s for s in self.statements if "ai_message" in s.lower()]

    def reset(self) -> None:
        self.statements.clear()


@pytest.fixture()
def counter():
    """监听真实应用引擎上的所有语句执行。"""
    counter = _QueryCounter()
    sync_engine = app_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", counter)
    yield counter
    event.remove(sync_engine, "before_cursor_execute", counter)


@pytest.fixture()
def history_user(client):
    """登录一个用户，返回 (user_id, headers)。"""
    resp = client.post("/api/login", json={"code": _CODE})
    assert resp.status_code == 200
    data = resp.json()["data"]
    return data["userId"], {"Authorization": f"Bearer {data['token']}"}


@pytest.fixture()
def seeded(engine, history_user):
    """100 条会话 × 20 条消息。"""
    user_id, headers = history_user
    seed_history_sync(engine, user_id)
    return user_id, headers


# --------------------------------------------------------------- 一、查询数与响应结构


def test_history_issues_exactly_two_statements(client, seeded, counter) -> None:
    """一次历史列表只对会话/消息表发 2 条语句：COUNT + 分页带预览。**

    改前是 1（COUNT）+ 1（分页）+ 20（逐条查消息）= 22 条。
    """
    _, headers = seeded
    counter.reset()

    resp = client.get("/api/ai/history?page=1&size=20", headers=headers)

    assert resp.status_code == 200
    assert len(resp.json()["data"]["list"]) == 20
    assert len(counter.ai_statements) == 2, "\n\n".join(counter.ai_statements)


def test_message_table_is_touched_once(client, seeded, counter) -> None:
    """`ai_message` 只在同一条分页 SQL 里出现一次，不存在"每条会话查一次"。"""
    _, headers = seeded
    counter.reset()

    client.get("/api/ai/history?page=1&size=20", headers=headers)

    assert len(counter.message_statements) == 1


def test_history_item_has_exactly_the_five_contract_fields(client, seeded) -> None:
    """响应字段就是契约本身：多出 `messages` 会让前端被迫处理大数据包。"""
    _, headers = seeded

    item = client.get("/api/ai/history?page=1&size=20", headers=headers).json()["data"]["list"][0]

    assert set(item) == {"id", "title", "preview", "messageCount", "updatedAt"}


def test_preview_is_last_message_truncated_to_30_chars(client, seeded) -> None:
    """`preview` 等于该会话最后一条消息的前 30 字符（不是第一条、不是全文）。"""
    _, headers = seeded

    data = client.get("/api/ai/history?page=1&size=20", headers=headers).json()["data"]
    # 首行是 update_time 最大的一条，即种数据时最后写入的那条（序号 99）
    first = data["list"][0]

    assert first["id"] == f"sess-perf-{CONVERSATION_COUNT - 1:03d}"
    assert first["preview"] == expected_preview(CONVERSATION_COUNT - 1)
    assert len(first["preview"]) == 30
    # 原文远长于 30 字符，取到整段说明截断没在库侧生效
    assert first["preview"] != last_message_content(CONVERSATION_COUNT - 1)


def test_message_count_matches_seeded_rows(client, seeded) -> None:
    """`messageCount` 是真实条数，而非预览条数或固定值。"""
    _, headers = seeded

    data = client.get("/api/ai/history?page=1&size=20", headers=headers).json()["data"]

    assert {item["messageCount"] for item in data["list"]} == {MESSAGES_PER_CONVERSATION}
    assert data["total"] == CONVERSATION_COUNT


def test_pagination_second_page_has_no_overlap(client, seeded) -> None:
    """第二页与第一页无重叠，且顺序按 `update_time` 倒序连续。"""
    _, headers = seeded

    first = client.get("/api/ai/history?page=1&size=20", headers=headers).json()["data"]["list"]
    second = client.get("/api/ai/history?page=2&size=20", headers=headers).json()["data"]["list"]

    assert len(second) == 20
    assert {i["id"] for i in first}.isdisjoint({i["id"] for i in second})
    # 倒序：第 21 条应比第 20 条旧
    assert second[0]["updatedAt"] < first[-1]["updatedAt"]


def test_conversation_without_messages_is_kept(client, engine, history_user, run_async) -> None:
    """无消息的会话仍出现在列表里：`messageCount=0`、`preview=""`（LEFT JOIN 不丢行）。"""
    user_id, headers = history_user

    async def _add_empty() -> None:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(
                insert(AIConversation),
                [{"id": "sess-empty-001", "user_id": user_id, "title": "空会话", "crop_tag": None}],
            )
            await db.commit()

    # 用 `engine`（NullPool）而非应用级引擎：后者是连接池，跨事件循环复用连接会报
    # "attached to a different loop"。NullPool 每次新建连接，与 `run_async` 相容。
    run_async(_add_empty())

    data = client.get("/api/ai/history?page=1&size=20", headers=headers).json()["data"]
    empty = next((item for item in data["list"] if item["id"] == "sess-empty-001"), None)

    assert empty is not None, "无消息的会话被 JOIN 丢掉了"
    assert empty["messageCount"] == 0
    assert empty["preview"] == ""
    assert empty["title"] == "空会话"


def test_oversized_page_size_is_truncated(client, seeded) -> None:
    """`size=100` 被截到上限 50：页大小由服务端兜底，不由前端决定。"""
    _, headers = seeded

    data = client.get("/api/ai/history?page=1&size=100", headers=headers).json()["data"]

    assert len(data["list"]) == _MAX_PAGE_SIZE
    # total 仍是真实总数，不被页大小影响
    assert data["total"] == CONVERSATION_COUNT


def test_size_zero_and_negative_are_clamped(client, seeded) -> None:
    """`size=0` 或负数不能变成"拉全部"，最少给 1 条。"""
    _, headers = seeded

    assert len(client.get("/api/ai/history?size=0", headers=headers).json()["data"]["list"]) == 1
    assert len(client.get("/api/ai/history?size=-5", headers=headers).json()["data"]["list"]) == 1


# --------------------------------------------------------------- 二、执行计划


async def _explain(db: AsyncSession, stmt) -> str:
    """对给定语句跑 `EXPLAIN (ANALYZE, BUFFERS)`，返回计划文本。"""
    sql = str(stmt.compile(db.bind, compile_kwargs={"literal_binds": True}))  # type: ignore[arg-type]
    result = await db.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"))
    return "\n".join(row[0] for row in result.all())


async def _plan_for(engine, user_id: str, *, strategy: str) -> str:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    build = conversation_repo.preview_lateral_stmt if strategy == "lateral" else conversation_repo.preview_stmt
    async with factory() as db:
        return await _explain(db, build(user_id, page=1, size=20))


@pytest.mark.parametrize("strategy", ["lateral"])
async def test_lateral_plan_avoids_seq_scan_on_message_table(engine, seeded, strategy) -> None:
    """`lateral` 策略下 `ai_message` 不走顺序扫描：每条会话按索引取最后一条。

    这是"默认用窗口函数"改为"默认用 LATERAL"的直接依据：窗口函数版的子查询
    没有 `conversation_id` 上的过滤条件，`Seq Scan on ai_message` 是必然出现的，
    而验收标准明确要求计划里不含它。
    """
    user_id, _ = seeded

    plan = await _plan_for(engine, user_id, strategy=strategy)

    assert "Seq Scan on ai_message" not in plan, plan
    assert "ai_message" in plan


async def test_window_plan_seq_scans_message_table(engine, seeded) -> None:
    """反向确认：窗口函数版确实全表扫 `ai_message`，故不适合作为默认策略。

    这条不是"必须失败"的断言，而是把两种策略的真实差异留在测试里：
    若将来有人把默认改回 `window`，这条会提示"它扫全表"。
    """
    user_id, _ = seeded

    plan = await _plan_for(engine, user_id, strategy="window")

    assert "Seq Scan on ai_message" in plan, plan


# --------------------------------------------------------------- 三、响应时间


def test_p95_latency_of_history_is_under_150ms(client, seeded) -> None:
    """100 会话 × 20 消息下，`/api/ai/history?size=20` 的 P95 小于 150ms。

    先跑一次预热：首请求包含连接建立与语句解析，把它算进 P95 会让断言变成
    对"连接池预热与否"的断言。
    """
    _, headers = seeded
    samples = 20

    client.get("/api/ai/history?page=1&size=20", headers=headers)

    elapsed: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        resp = client.get("/api/ai/history?page=1&size=20", headers=headers)
        elapsed.append(time.perf_counter() - start)
        assert resp.status_code == 200

    elapsed.sort()
    p95 = elapsed[int(samples * 0.95) - 1]

    assert p95 < 0.150, f"P95={p95 * 1000:.1f}ms，样本={[round(e * 1000, 1) for e in elapsed]}"


# --------------------------------------------------------------- 四、种数据的自检


async def test_seeded_data_has_expected_shape(engine, history_user) -> None:
    """先证明夹具本身是对的：100 会话、2000 消息、更新时间严格递增。

    夹具错了会让上面所有断言失去意义（比如 `update_time` 全相等时，排序断言
    会随机通过）。
    """
    user_id, _ = history_user
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await seed_history(db, user_id)
        conv_times = (
            (
                await db.execute(
                    text("SELECT update_time FROM ai_conversation WHERE user_id = :uid ORDER BY update_time"),
                    {"uid": user_id},
                )
            )
            .scalars()
            .all()
        )
        msg_count = (await db.execute(text("SELECT count(*) FROM ai_message"))).scalar_one()

    assert len(conv_times) == CONVERSATION_COUNT
    assert len(set(conv_times)) == CONVERSATION_COUNT, "更新时间必须互不相同，否则排序断言无意义"
    assert conv_times == sorted(conv_times), "夹具必须递增写入，列表倒序才有确定顺序"
    assert msg_count == CONVERSATION_COUNT * MESSAGES_PER_CONVERSATION
