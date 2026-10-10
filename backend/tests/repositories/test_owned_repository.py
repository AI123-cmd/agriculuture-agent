"""T1-05 验收：仓储基类的归属安全。

四类断言各对应一条越权路径：缺归属参数、跨用户读、跨用户写、子表经父会话间接归属。
这些用例跑在真实 PostgreSQL 上（`engine` 由 T1-04 的迁移建出表）。
"""

import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.ownership import MissingOwnerError, OwnershipError
from app.db.base import new_id
from app.models import AIConversation, AIMessage, User
from app.repositories import ConversationRepository, conversation_repo, message_repo
from app.repositories.base import OwnedRepository

BACKEND_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.usefixtures("requires_postgres")

# 应继承 OwnedMixin 的 9 张业务表；user 是归属主体自身，ai_message 无 user_id
OWNED_MODEL_FILES = (
    "farmer_profile.py",
    "crop.py",
    "field.py",
    "device.py",
    "location.py",
    "ledger.py",
    "conversation.py",
    "memory_summary.py",
    "profile_tag.py",
)


@pytest.fixture()
async def two_users(engine) -> dict[str, str]:
    """建两个用户 A / B。跨用户断言全部基于这一对 id。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    ids: dict[str, str] = {}
    async with factory() as db:
        for label in ("a", "b"):
            user = User(id=new_id(f"user_{label}"), openid=f"openid-{label}", gender=0)
            db.add(user)
            await db.flush()
            ids[label] = user.id
        await db.commit()
    return ids


async def _count_conversations(engine) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        return (await db.execute(select(func.count()).select_from(AIConversation))).scalar_one()


async def test_missing_user_id_is_rejected(engine, two_users) -> None:
    """空串与非字符串一律抛 MissingOwnerError，不做降级为"查全部"。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(MissingOwnerError):
            await conversation_repo.get(db, "", "any-id")
        with pytest.raises(MissingOwnerError):
            await conversation_repo.get(db, None, "any-id")  # type: ignore[arg-type]
        with pytest.raises(MissingOwnerError):
            await conversation_repo.count(db, None)  # type: ignore[arg-type]


async def test_missing_owner_error_is_subclass_of_ownership_error() -> None:
    """两者在日志里可区分，但对上层都是 403。"""
    assert issubclass(MissingOwnerError, OwnershipError)
    assert OwnershipError().code == 403


async def test_cross_user_get_is_invisible(engine, two_users) -> None:
    """A 建的记录，B 查不到；B 用自己的 id 查得到。"""
    conv_id = new_id("sess")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await conversation_repo.add(db, two_users["a"], AIConversation(id=conv_id, title="A 的会话"))
        await db.commit()

    async with factory() as db:
        assert await conversation_repo.get(db, two_users["b"], conv_id) is None
        with pytest.raises(OwnershipError):
            await conversation_repo.get_or_raise(db, two_users["b"], conv_id)
        owned = await conversation_repo.get(db, two_users["a"], conv_id)
        assert owned is not None and owned.id == conv_id


async def test_add_rejects_foreign_owner_without_inserting(engine, two_users) -> None:
    """传入他人的 user_id 时抛异常，且一行都不落库。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        intruder = AIConversation(id=new_id("sess"), user_id=two_users["b"], title="冒名")
        with pytest.raises(OwnershipError):
            await conversation_repo.add(db, two_users["a"], intruder)
        await db.rollback()

    assert await _count_conversations(engine) == 0


async def test_add_forces_owner_when_unset(engine, two_users) -> None:
    """对象未带 user_id 时由仓储强制写入，调用方无法"忘记归属"。"""
    conv_id = new_id("sess")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        conv = AIConversation(id=conv_id, title="新会话")
        await conversation_repo.add(db, two_users["a"], conv)
        assert conv.user_id == two_users["a"]
        await db.commit()

    async with factory() as db:
        stored = await conversation_repo.get(db, two_users["a"], conv_id)
        assert stored is not None


async def test_update_and_delete_are_scoped_to_owner(engine, two_users) -> None:
    """更新与删除按归属过滤：B 动手时影响 0 行，A 的数据不变。"""
    conv_id = new_id("sess")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await conversation_repo.add(db, two_users["a"], AIConversation(id=conv_id, title="原标题"))
        await db.commit()

    async with factory() as db:
        assert await conversation_repo.update_fields(db, two_users["b"], conv_id, title="被改") == 0
        assert await conversation_repo.delete(db, two_users["b"], conv_id) == 0
        await db.commit()

    async with factory() as db:
        stored = await conversation_repo.get(db, two_users["a"], conv_id)
        assert stored is not None and stored.title == "原标题"


async def test_delete_with_messages_requires_ownership(engine, two_users) -> None:
    """B 删 A 的会话抛 OwnershipError，且 A 的会话与消息都还在。"""
    conv_id = new_id("sess")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await conversation_repo.add(db, two_users["a"], AIConversation(id=conv_id, title="A 的会话"))
        db.add(AIMessage(conversation_id=conv_id, role="user", content="你好"))
        db.add(AIMessage(conversation_id=conv_id, role="ai", content="你好，请描述症状"))
        await db.commit()

    async with factory() as db:
        with pytest.raises(OwnershipError):
            await conversation_repo.delete_with_messages(db, two_users["b"], conv_id)
        await db.rollback()

    async with factory() as db:
        assert await conversation_repo.get(db, two_users["a"], conv_id) is not None
        remaining = await message_repo.list_by_conversation(db, two_users["a"], conv_id)
        assert len(remaining) == 2


async def test_delete_with_messages_removes_child_then_parent(engine, two_users) -> None:
    """A 删自己的会话：消息与会话一并消失，不留孤儿。"""
    conv_id = new_id("sess")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        await conversation_repo.add(db, two_users["a"], AIConversation(id=conv_id, title="A 的会话"))
        db.add(AIMessage(conversation_id=conv_id, role="user", content="你好"))
        await db.commit()

    async with factory() as db:
        assert await conversation_repo.delete_with_messages(db, two_users["a"], conv_id) == 1
        await db.commit()

    async with factory() as db:
        orphans = (await db.execute(select(func.count()).select_from(AIMessage))).scalar_one()
        assert orphans == 0


async def test_scoped_repository_deletes_only_own_messages(engine, two_users) -> None:
    """`delete_by_user` 只删本人会话下的消息。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for label in ("a", "b"):
            conv_id = f"sess-{label}"
            await conversation_repo.add(db, two_users[label], AIConversation(id=conv_id, title=f"{label} 的会话"))
            db.add(AIMessage(conversation_id=conv_id, role="user", content=f"{label} 的消息"))
        await db.commit()

    async with factory() as db:
        assert await message_repo.delete_by_user(db, two_users["a"]) == 1
        await db.commit()

    async with factory() as db:
        left = (await db.execute(select(AIMessage.conversation_id))).scalars().all()
        assert left == ["sess-b"]


async def test_scoped_repository_rejects_missing_owner(engine) -> None:
    """子表仓储同样不接受空归属。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(MissingOwnerError):
            await message_repo.delete_by_user(db, "")


async def test_conversation_repository_is_owned_repository_instance() -> None:
    """`ConversationRepository` 必须继承基类，不得自带裸查询方法。"""
    assert isinstance(conversation_repo, OwnedRepository)
    assert isinstance(conversation_repo, ConversationRepository)
    assert conversation_repo.model is AIConversation


def test_repository_base_has_no_escape_hatch() -> None:
    """基类不得提供返回裸 Session 或原始语句的出口。

    与验收命令等价的三条匹配：`def .*session` / `def raw` / `def query`。
    """
    source = (BACKEND_ROOT / "app" / "repositories" / "base.py").read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if any(re.search(pattern, line) for pattern in (r"def .*session", r"def raw", r"def query"))
    ]
    assert offenders == [], f"仓储基类出现逃生舱：{offenders}"


def test_nine_business_models_inherit_owned_mixin() -> None:
    """9 张业务表继承 OwnedMixin；user.py 为 0（归属主体自身不继承）。"""
    models_dir = BACKEND_ROOT / "app" / "models"
    for filename in OWNED_MODEL_FILES:
        content = (models_dir / filename).read_text(encoding="utf-8")
        assert "OwnedMixin" in content, f"{filename} 未继承 OwnedMixin"
    assert "OwnedMixin" not in (models_dir / "user.py").read_text(encoding="utf-8")


def test_repositories_pass_mypy() -> None:
    """泛型 ModelT 不产生 Any 泄露（验收要求 mypy 通过）。"""
    python = BACKEND_ROOT / ".venv" / "Scripts" / "python.exe"
    executable = str(python) if python.exists() else "python"
    result = subprocess.run(
        [executable, "-m", "mypy", "app/repositories"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"mypy app/repositories 失败：{result.stdout}{result.stderr}"
