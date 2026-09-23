"""跨用户测试夹具：两个用户 A / B 及各自名下的资源。

刻意不用 HTTP 登录建用户：登录链路本身由 `tests/test_auth.py` 覆盖，这里要的是
"库中已存在两套互不相关的数据"，用直接建行最稳，也不受 token 实现变化影响。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AIConversation,
    AIMessage,
    BroadcastLog,
    Crop,
    Device,
    FarmerProfile,
    Location,
    MemorySummary,
    User,
    UserProfileTag,
)

# 播报按"今天"落库：`/api/broadcast/today` 只查当日，用今天才能让"跨用户不可见"
# 这条读断言真正有区分度（否则 B 的条目本来就不在结果里，断言等于没测）。
BROADCAST_DATE = date.today().isoformat()


@dataclass
class TwoUsers:
    """A / B 两个用户及其名下资源 id 的集合。"""

    a: str
    b: str
    session_factory: async_sessionmaker[AsyncSession]
    a_conversation: str = ""
    b_conversation: str = ""
    a_broadcast: str = ""
    b_broadcast: str = ""
    a_device: str = ""
    b_device: str = ""
    a_tag: str = ""
    b_tag: str = ""
    a_crop: str = ""
    extra: dict = field(default_factory=dict)

    def session(self) -> AsyncSession:
        return self.session_factory()


async def _seed_user(db: AsyncSession, key: str, label: str) -> User:
    user = User(id=f"user-{key}", openid=f"openid-{key}", nick_name=f"{label}的昵称", gender=0)
    db.add(user)
    await db.flush()
    return user


async def _seed_for(db: AsyncSession, user_id: str, *, key: str, label: str) -> dict:
    """为单个用户名下建齐各类资源，返回 id 集合。

    `key` 用于拼主键（纯 ASCII，便于断言里直接引用），`label` 用于可读内容
    （断言"看到的是自己那条"时依赖它具备区分度）。
    """
    conv_id = f"sess-{key}"
    conv = AIConversation(id=conv_id, user_id=user_id, title=f"{label}的会话", crop_tag="水稻")
    db.add(conv)
    await db.flush()
    db.add(AIMessage(conversation_id=conv_id, role="user", content=f"{label}的问句"))
    db.add(AIMessage(conversation_id=conv_id, role="ai", content=f"{label}的答复"))

    db.add(
        FarmerProfile(
            id=f"fp-{key}",
            user_id=user_id,
            real_name=f"{label}农户",
            province="江西省",
            city="抚州市",
            district="临川区",
            total_mu=10,
        )
    )
    db.add(
        Crop(
            id=f"crop-{key}",
            user_id=user_id,
            crop_name="水稻",
            plant_mu=10,
            growth_stage="分蘖期",
            is_main=1,
        )
    )
    db.add(UserProfileTag(id=f"tag-{key}", user_id=user_id, tags=[f"{label}的标签"], chat_count=1))
    db.add(
        Device(
            id=f"dev-{key}",
            user_id=user_id,
            device_sn=f"SN-{key}",
            device_type="soil",
            device_name=f"{label}的设备",
            status=1,
        )
    )
    db.add(
        BroadcastLog(
            id=f"bc-{key}",
            user_id=user_id,
            date=BROADCAST_DATE,
            time="09:00",
            title=f"{label}的播报",
            content=f"{label}的播报内容",
            type="info",
            level="normal",
            source="sensor",
            done=0,
        )
    )
    db.add(
        Location(
            id=f"loc-{key}",
            user_id=user_id,
            location_name=f"{label}的地块",
            source="manual",
        )
    )
    db.add(
        MemorySummary(
            id=f"mem-{key}",
            user_id=user_id,
            session_id=conv_id,
            summary=f"{label}的摘要",
            key_facts=[f"{label}的事实"],
        )
    )
    return {
        "conversation": conv_id,
        "profile": f"fp-{key}",
        "crop": f"crop-{key}",
        "tag": f"tag-{key}",
        "device": f"dev-{key}",
        "broadcast": f"bc-{key}",
        "location": f"loc-{key}",
        "memory": f"mem-{key}",
    }


@pytest.fixture()
async def two_users(engine) -> AsyncIterator[TwoUsers]:
    """建 A / B 两套完整数据，供跨用户读写断言。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await _seed_user(db, "a", "甲")
        await _seed_user(db, "b", "乙")
        ids_a = await _seed_for(db, "user-a", key="a", label="甲")
        ids_b = await _seed_for(db, "user-b", key="b", label="乙")
        await db.commit()

    yield TwoUsers(
        a="user-a",
        b="user-b",
        session_factory=factory,
        a_conversation=ids_a["conversation"],
        b_conversation=ids_b["conversation"],
        a_broadcast=ids_a["broadcast"],
        b_broadcast=ids_b["broadcast"],
        a_device=ids_a["device"],
        b_device=ids_b["device"],
        a_tag=ids_a["tag"],
        b_tag=ids_b["tag"],
        a_crop=ids_a["crop"],
        extra={"a": ids_a, "b": ids_b},
    )


__all__ = ["BROADCAST_DATE", "TwoUsers", "two_users"]
