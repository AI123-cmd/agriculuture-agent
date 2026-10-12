"""跨用户测试夹具：两个用户 A / B 及各自名下的资源。

刻意不用 HTTP 登录建用户：登录链路本身由 `tests/test_auth.py` 覆盖，这里要的是
"库中已存在两套互不相关的数据"，用直接建行最稳，也不受 token 实现变化影响。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AIConversation,
    AIMessage,
    Crop,
    DeviceConfig,
    FarmerProfile,
    Location,
    MemorySummary,
    User,
    UserProfileTag,
)


@dataclass
class TwoUsers:
    """A / B 两个用户及其名下资源 id 的集合。"""

    a: str
    b: str
    session_factory: async_sessionmaker[AsyncSession]
    a_conversation: str = ""
    b_conversation: str = ""
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
    # 设备建在 `device_config`（绑定、上报、解绑都写这张表，也是环境详情/设备列表的唯一准据）。
    # 旧 `device` 表只服务历史数据，新功能不再读它（T5-09 第六条）。
    db.add(
        DeviceConfig(
            id=f"dev-{key}",
            user_id=user_id,
            device_sn=f"SN-{key}",
            protocol_type="http_direct",
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
        a_device=ids_a["device"],
        b_device=ids_b["device"],
        a_tag=ids_a["tag"],
        b_tag=ids_b["tag"],
        a_crop=ids_a["crop"],
        extra={"a": ids_a, "b": ids_b},
    )


__all__ = ["TwoUsers", "two_users"]
