"""T1-09 验收：显式事务边界（`unit_of_work`）与请求级会话回滚。

任务文件实现要点六的六条断言逐条落在这里：

1. `uow` 内写入后抛异常 → 该行未落库（**用新会话查询**，不信当前会话的身份映射缓存）。
2. 嵌套内层失败只回滚内层，外层照常提交。
3. 连续两次 `uow` 都提交成功（证明是"各自提交"，不是"只剩最后一次"）。
4. 唯一约束冲突转成 `ConflictError`（409）：接口层拿 409，日志里没有 SQL 与表名。
5. 服务层漏提交时 `get_db` 的 `finally` 分支回滚并告警（fail-closed）。
6. 业务异常（403 一类）不被包装成 `TransactionError`(500)。

这些断言依赖 PostgreSQL 的 `SAVEPOINT` 与错误恢复语义（`asyncpg` 在 `IntegrityError`
后必须靠回滚到保存点恢复会话），SQLite 不等价，故整份文件走真实 PG。
"""

import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.core.exceptions import AppException, ConflictError, TransactionError
from app.core.ownership import OwnershipError
from app.db.session import get_db
from app.db.uow import unit_of_work
from app.main import app_exception_handler, unhandled_exception_handler
from app.models import AIConversation, Device
from app.repositories import conversation_repo, device_repo

pytestmark = pytest.mark.usefixtures("requires_postgres")

# `ai_conversation.user_id` 与 `device.user_id` 都无外键，不必先建 user 行
_USER = "user-tx-owner"
# 唯一约束目标：`device.device_sn` 上有 `ix_device_device_sn`（unique）
_SN = "SN-TX-DUPLICATE-0001"


async def _count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    """新开会话统计行数：当前会话可能持有未提交的待写对象，不能作数。"""
    async with factory() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _conversation_ids(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as db:
        return list((await db.execute(select(AIConversation.id))).scalars().all())


# ----------------------------------------------------------------- 一、异常即回滚


async def test_exception_inside_uow_rolls_back(session_factory) -> None:
    """`uow` 内写入后抛异常：该行一行都不落库。"""
    async with session_factory() as db:
        with pytest.raises(TransactionError):
            async with unit_of_work(db, name="test.rollback"):
                await conversation_repo.add(db, _USER, AIConversation(id="sess-tx-rollback", title="将被回滚"))
                raise RuntimeError("业务中途失败")

    assert await _count(session_factory, AIConversation) == 0


async def test_business_exception_is_not_wrapped(session_factory) -> None:
    """业务异常原样上抛：归属校验的 403 不得退化成 500。"""
    async with session_factory() as db:
        with pytest.raises(OwnershipError) as exc_info:
            async with unit_of_work(db, name="test.ownership"):
                raise OwnershipError("资源不存在或无权访问")

    assert exc_info.value.code == 403
    assert await _count(session_factory, AIConversation) == 0


# ----------------------------------------------------------------- 二、嵌套只回滚内层


async def test_nested_inner_failure_rolls_back_inner_only(session_factory) -> None:
    """内层失败回滚到保存点，外层捕获后继续并提交成功。"""
    async with session_factory() as db:
        async with unit_of_work(db, name="test.outer"):
            await conversation_repo.add(db, _USER, AIConversation(id="sess-outer-ok", title="外层"))
            # 外层这一行先落库：确保它不在 SAVEPOINT 窗口内，才能证明"只回滚内层"
            await db.flush()

            with pytest.raises(RuntimeError):
                async with unit_of_work(db, name="test.inner"):
                    await conversation_repo.add(db, _USER, AIConversation(id="sess-inner-fail", title="内层"))
                    # 内层这一行真的进了当前事务，回滚保存点才会把它丢掉
                    await db.flush()
                    raise RuntimeError("内层写入失败")

    assert await _conversation_ids(session_factory) == ["sess-outer-ok"]


# ----------------------------------------------------------------- 三、两次事务都生效


async def test_two_sequential_uows_both_persist(session_factory) -> None:
    """连续两次 `uow` 各自提交：两行都在，不是"只剩最后一次"。"""
    async with session_factory() as db:
        async with unit_of_work(db, name="test.first"):
            await conversation_repo.add(db, _USER, AIConversation(id="sess-seq-1", title="第一次"))
        async with unit_of_work(db, name="test.second"):
            await conversation_repo.add(db, _USER, AIConversation(id="sess-seq-2", title="第二次"))

    assert sorted(await _conversation_ids(session_factory)) == ["sess-seq-1", "sess-seq-2"]


async def test_depth_marker_resets_after_each_uow(session_factory) -> None:
    """嵌套深度标记用完即归零：否则第二次调用会被误判成嵌套而永不提交。"""
    async with session_factory() as db:
        async with unit_of_work(db, name="test.marked"):
            await conversation_repo.add(db, _USER, AIConversation(id="sess-marked", title="标记"))
        assert db.info.get("uow_depth", 0) == 0
        assert db.in_transaction() is False, "提交后不应留下隐式事务"

    assert await _count(session_factory, AIConversation) == 1


# ----------------------------------------------------------------- 四、并发冲突 → 409


async def test_unique_violation_raises_conflict_error(session_factory) -> None:
    """重复 `device_sn` 触发 `ConflictError`(409)，不是 500。"""
    async with session_factory() as db:
        async with unit_of_work(db, name="test.device.first"):
            await device_repo.add(db, _USER, Device(id="dev-tx-1", device_sn=_SN, device_type="soil"))

    async with session_factory() as db:
        with pytest.raises(ConflictError) as exc_info:
            async with unit_of_work(db, name="test.device.duplicate"):
                await device_repo.add(db, _USER, Device(id="dev-tx-2", device_sn=_SN, device_type="soil"))

    assert exc_info.value.code == 409
    assert await _count(session_factory, Device) == 1


def _probe_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """探针应用：复用生产异常处理器，只把会话与"当前用户"换成测试版本。

    仓库里还没有创建设备的接口（`device` 目前只有读链路），而验收要求在**接口层**
    观察 409，故用一条探针路由承载 `unit_of_work` + 唯一约束冲突，与
    `tests/test_ownership_403.py` 的做法一致。
    """
    probe = FastAPI()
    probe.add_exception_handler(AppException, app_exception_handler)
    probe.add_exception_handler(Exception, unhandled_exception_handler)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def _current_user() -> str:
        return _USER

    probe.dependency_overrides[get_db] = _session
    probe.dependency_overrides[get_current_user] = _current_user

    @probe.post("/probe/device")
    async def create_device(
        payload: dict,
        db: AsyncSession = Depends(get_db),
        user_id: str = Depends(get_current_user),
    ) -> dict:
        async with unit_of_work(db, name="probe.device"):
            await device_repo.add(
                db,
                user_id,
                Device(
                    id=payload["id"],
                    device_sn=payload["sn"],
                    device_type=payload.get("type", "soil"),
                ),
            )
        return {"id": payload["id"]}

    return probe


async def test_conflict_surfaces_as_http_409_without_sql_leak(session_factory, caplog) -> None:
    """接口层拿 409 与 `code=409`；响应体与日志都不含原始 SQL、表名与约束名。"""
    app = _probe_app(session_factory)
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as http:
            first = await http.post("/probe/device", json={"id": "dev-http-1", "sn": _SN})
            second = await http.post("/probe/device", json={"id": "dev-http-2", "sn": _SN})

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == 409

    leaked = ("insert into", "device_sn", "duplicate key", "asyncpg", "ix_device")
    haystack = f"{second.text}\n{caplog.text}".lower()
    assert [token for token in leaked if token in haystack] == []
    assert await _count(session_factory, Device) == 1


# ------------------------------------------------- 五、漏提交由请求级会话兜底回滚


async def test_get_db_rolls_back_forgotten_commit(session_factory, monkeypatch, caplog) -> None:
    """模拟"服务层忘记提交"：请求级会话在收尾时回滚并留下可检索的告警。

    `SessionLocal` 被换成测试库工厂，否则这条用例会连到开发库。
    """
    monkeypatch.setattr("app.db.session.SessionLocal", session_factory)

    gen = get_db()
    db = await anext(gen)
    await conversation_repo.add(db, _USER, AIConversation(id="sess-forgot-commit", title="忘记提交"))
    # 下推 INSERT：证明"确实写过库"，否则回滚一说无从谈起
    await db.flush()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(StopAsyncIteration):
            await anext(gen)

    assert "已回滚" in caplog.text
    assert "漏了提交" in caplog.text
    assert await _count(session_factory, AIConversation) == 0


async def test_get_db_does_not_commit_for_service(session_factory, monkeypatch) -> None:
    """兜底分支不得替服务层提交：写入必须消失，而不是被静默落库。"""
    monkeypatch.setattr("app.db.session.SessionLocal", session_factory)

    gen = get_db()
    db = await anext(gen)
    await conversation_repo.add(db, _USER, AIConversation(id="sess-must-not-persist", title="不该留下"))
    await db.flush()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)

    assert await _conversation_ids(session_factory) == []
