"""T2-30：PostgreSQL 检查点器的单例、跨循环复用、setup 幂等与中断恢复。

替换 SQLite 检查点的核心收益是"连接池不绑事件循环"与"状态持久在 PG 可跨进程重启存活"，
本文件按验收标准逐条钉住这两点：

1. 单例：连续两次 `get_checkpointer()` 返回同一对象；
2. 跨循环：在两个**不同**事件循环里分别调用，返回同一 saver 且不抛异常
   （旧 `_saver_loop` 实现在这里会重建，故这条断言正是新旧实现的分水岭）；
3. `setup()` 幂等：连续两次调用不报错（已存在表则跳过）；
4. 中断恢复：同一 `thread_id` 跑一次部分链路 → `close_checkpointer()` → 重建
   → `slots` / 进度不丢，可从断点续答；
5. 收尾：`close_checkpointer()` 释放池，`pg_stat_activity` 不留残留空闲连接；
6. 旧实现下线：`app/` 内不再出现 `AsyncSqliteSaver` / `aiosqlite`。

中断恢复用最小可复现图（真实 interview 节点 + 条件边 + 真实 Postgres saver）驱动，
问诊智能体用替身固定，避开幕模型与候选池的抖动——与 T2-32 的 `test_interrupt.py`
同构，但这里刻意走"关闭并重建检查点器"的路径，验证的是**持久化**而非图实例复用。
"""

import asyncio
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy import text

from app.agent.budget import ConsultBudget
from app.agent.checkpointer import close_checkpointer, get_checkpointer
from app.agent.contracts import InterviewBatch, InterviewQuestion
from app.agent.graph import factories
from app.agent.graph.routes import ROUTE_AFTER_INTERVIEW, route_after_interview
from app.agent.state import ConsultStage, ConsultationState, ProcessId
from app.process_config.loader import get_process_config

_INTERVIEW_ROUTES = {key: value for key, value in ROUTE_AFTER_INTERVIEW.items() if key != "panel"}


class _FakeInterviewer:
    """固定问某个槽位的问题，绕开模型与候选池，让断言只看中断与持久化行为。"""

    def __init__(self, slot_key: str) -> None:
        self._slot_key = slot_key

    async def run(self, state: ConsultationState, ctx: Any) -> InterviewBatch:
        return InterviewBatch(
            questions=[
                InterviewQuestion(
                    slot_key=self._slot_key,
                    text="叶片是什么样子的？",
                    why="看清症状才能判断",
                    options=["发黄", "说不清"],
                    answer_type="single",
                )
            ],
            stage_hint="正在了解情况",
        )


def _patch_interviewer(monkeypatch, slot_key: str) -> None:
    """把工厂模块里的 `InterviewerAgent` 换成固定问题的替身。

    `make_interview_node` 在**建造节点时**读模块全局，故替换必须在建图之前。
    """
    monkeypatch.setattr(factories, "InterviewerAgent", lambda *a, **k: _FakeInterviewer(slot_key))


async def _verify_stub(state: ConsultationState) -> dict:
    return {"stage": ConsultStage.VERIFY}


def _interview_app(checkpointer):
    """最小问诊图：interview（真实节点）→ route_after_interview → verify / 回环。"""
    config = get_process_config(ProcessId.RICE)
    graph = StateGraph(ConsultationState)
    graph.add_node("interview", factories.make_interview_node(config))
    graph.add_node("verify", _verify_stub)
    graph.add_edge(START, "interview")
    graph.add_conditional_edges("interview", route_after_interview, _INTERVIEW_ROUTES)
    graph.add_edge("verify", END)
    return graph.compile(checkpointer=checkpointer)


def _interview_state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-t2-30",
        "redlines_loaded": True,
        "required_slots": [],
        "slots": {},
        "budget": ConsultBudget().to_state(),
        "round": 0,
    }
    base.update(overrides)
    return base


def _interrupt_payload(result: dict) -> dict:
    interrupts = result.get("__interrupt__")
    assert interrupts, f"预期出现中断，实际结果：{result}"
    return interrupts[0].value


# ---- 1. 单例 ----


async def test_get_checkpointer_returns_singleton(requires_postgres) -> None:
    """连续两次调用返回同一 saver 对象，且关闭后可重建为新对象。"""
    first = await get_checkpointer()
    second = await get_checkpointer()
    assert first is second

    await close_checkpointer()
    third = await get_checkpointer()
    assert third is not first
    await close_checkpointer()


# ---- 2. 跨事件循环复用 ----


def test_saver_reused_across_event_loops(requires_postgres) -> None:
    """两个不同事件循环里分别取用，得到同一 saver 且不抛异常。

    旧 SQLite 实现用 `_saver_loop` 判断循环变化后重建，这里会得到不同对象；
    Postgres 连接池不绑循环，故返回同一对象。池在首个循环里开、也在首个循环里关，
    第二个循环只读取缓存对象，避免跨循环关闭连接。
    """
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        first = loop_a.run_until_complete(get_checkpointer())
        second = loop_b.run_until_complete(get_checkpointer())
        assert first is second
    finally:
        loop_a.run_until_complete(close_checkpointer())
        loop_a.close()
        loop_b.close()


# ---- 3. setup 幂等 ----


async def test_setup_is_idempotent(requires_postgres) -> None:
    """已建表时再次 `setup()` 不报错。"""
    saver = await get_checkpointer()
    await saver.setup()
    await saver.setup()
    await close_checkpointer()


# ---- 4. 中断恢复：关闭并重建检查点器后进度不丢 ----


async def test_question_flow_resumes_after_reopening_checkpointer(monkeypatch, requires_postgres) -> None:
    """同一 thread_id 从断点续答，`slots` 与轮次在检查点器重建后仍不丢。"""
    _patch_interviewer(monkeypatch, "symptom")
    config = {"configurable": {"thread_id": "t-t2-30-resume"}}
    state = _interview_state(required_slots=["symptom", "other"])

    saver = await get_checkpointer()
    app1 = _interview_app(saver)
    first = await app1.ainvoke(state, config)
    assert _interrupt_payload(first)["progress"] == {"current": 1, "total": 4}

    after_one = await app1.ainvoke(Command(resume={"answers": [{"slotKey": "symptom", "value": "x"}]}), config)
    assert after_one["slots"]["symptom"].value == "x"
    assert after_one["budget"]["interview_rounds"] == 1
    assert _interrupt_payload(after_one)["progress"]["current"] == 2  # 仍有未答槽位，再次中断

    # 关闭并重建检查点器（等价于进程重启）：状态应仍持久在 PostgreSQL
    await close_checkpointer()
    saver2 = await get_checkpointer()
    app2 = _interview_app(saver2)

    snapshot = await app2.aget_state(config)
    assert snapshot.next  # 中断仍在，未被当成新会话
    assert snapshot.values["slots"]["symptom"].value == "x"  # 已答槽位未丢
    assert snapshot.values["budget"]["interview_rounds"] == 1

    resumed = await app2.ainvoke(Command(resume={"answers": [{"slotKey": "other", "value": "y"}]}), config)
    assert resumed["slots"]["symptom"].value == "x"
    assert resumed["slots"]["other"].value == "y"
    assert resumed["budget"]["interview_rounds"] == 2
    assert resumed["stage"] == ConsultStage.VERIFY

    await close_checkpointer()


# ---- 5. 收尾：不留残留空闲连接 ----


async def test_close_checkpointer_releases_connections(engine, requires_postgres) -> None:
    """`close_checkpointer()` 归还池内连接，`pg_stat_activity` 回到基线。"""

    async def _count() -> int:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            )
            return int(result.scalar_one())

    baseline = await _count()

    await get_checkpointer()  # 打开池，min_size=2，连接数应上升
    during = await _count()
    assert during > baseline

    await close_checkpointer()
    after = during
    for _ in range(10):
        after = await _count()
        if after <= baseline:
            break
        await asyncio.sleep(0.1)
    assert after <= baseline


# ---- 6. 旧实现彻底下线 ----


def test_legacy_sqlite_checkpointer_is_removed() -> None:
    """`app/` 内不得残留 `AsyncSqliteSaver` / `aiosqlite` 引用。"""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        str(path)
        for path in app_dir.rglob("*.py")
        if "AsyncSqliteSaver" in path.read_text(encoding="utf-8") or "aiosqlite" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
