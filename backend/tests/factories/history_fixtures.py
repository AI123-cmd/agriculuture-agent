"""历史列表的批量测试数据：100 条会话 × 20 条消息。

用批量 `INSERT` 而非逐条 `db.add`：单条 add 在 2000 行时会走 2000 次 ORM 刷新，
慢到让"性能测试"本身成为瓶颈，反而看不出 SQL 侧的差异。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AIConversation, AIMessage

# 会话标题与消息内容都用固定格式，便于断言"取到的是最后一条消息"
CONVERSATION_COUNT = 100
MESSAGES_PER_CONVERSATION = 20

# 会话 `update_time` 的基准与步长。必须显式写入而不能靠 `server_default=now()`：
# 同一条 INSERT 里 `now()` 返回事务开始时间，100 行会得到完全相同的时间戳，
# 而列表按 `update_time DESC` 排序——排序退化成任意顺序，翻页断言就成了掷骰子。
_TIME_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TIME_STEP = timedelta(minutes=1)


def conversation_ids(*, count: int = CONVERSATION_COUNT) -> list[str]:
    """会话 id 形如 `sess-perf-000`，补零保证字典序与创建序一致。"""
    return [f"sess-perf-{index:03d}" for index in range(count)]


def update_time_at(index: int) -> datetime:
    """第 `index` 条会话的更新时间：序号越大越新，故列表首行是最后一条。"""
    return _TIME_BASE + _TIME_STEP * index


def last_message_content(index: int) -> str:
    """第 `index` 条会话的最后一条消息内容（每条都不同，便于核对归属）。

    刻意远长于 30 字符：预览必须在 SQL 侧截断，本条断言能证明"没有整段传回"。
    """
    return f"最后一条-{index:03d}-" + "长内容" * 40


def expected_preview(index: int) -> str:
    """第 `index` 条会话的预览：其最后一条消息的前 30 字符。"""
    return last_message_content(index)[:30]


async def seed_history(
    db: AsyncSession,
    user_id: str,
    *,
    conversations: int = CONVERSATION_COUNT,
    messages_per: int = MESSAGES_PER_CONVERSATION,
) -> list[str]:
    """为 `user_id` 造 `conversations × messages_per` 条数据，返回会话 id 列表（由旧到新）。

    每条会话的 `update_time` 显式递增（见 `update_time_at`），列表按它倒序，
    因此首行是最后一条会话——翻页断言依赖这个确定顺序。
    """
    ids = conversation_ids(count=conversations)
    await db.execute(
        insert(AIConversation),
        [
            {
                "id": conv_id,
                "user_id": user_id,
                "title": f"会话{index}",
                "crop_tag": "水稻",
                "create_time": update_time_at(index),
                "update_time": update_time_at(index),
            }
            for index, conv_id in enumerate(ids)
        ],
    )

    rows: list[dict] = []
    for index, conv_id in enumerate(ids):
        for seq in range(messages_per):
            rows.append(
                {
                    "conversation_id": conv_id,
                    "role": "ai" if seq % 2 else "user",
                    # 只有最后一条需要长内容，前面的用短内容减少插入量
                    "content": last_message_content(index) if seq == messages_per - 1 else f"第{seq}轮-{index}",
                }
            )
    await db.execute(insert(AIMessage), rows)
    await db.commit()
    return ids


def seed_history_sync(engine, user_id: str, **kwargs) -> list[str]:
    """同步入口：供 `TestClient` 这类在别的循环里跑的用例准备数据。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _seed() -> list[str]:
        async with factory() as db:
            return await seed_history(db, user_id, **kwargs)

    return asyncio.run(_seed())


__all__ = [
    "CONVERSATION_COUNT",
    "MESSAGES_PER_CONVERSATION",
    "conversation_ids",
    "expected_preview",
    "last_message_content",
    "seed_history",
    "seed_history_sync",
]
