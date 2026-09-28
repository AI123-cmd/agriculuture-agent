"""T2-32：两类 interrupt 与三个恢复场景的集成测试。

本文件覆盖编排层的中断/恢复语义，用**最小可复现图**而不是整条主图来驱动：中断的落点是
`make_interview_node`（等农户回答）与 `build_gate_node`（安全拦截后确认），把这两个真实节点
配上一两条条件边与检查点，就能精确断言"在哪中断、恢复后走到哪、预算有没有被重置"，而
不必依赖分诊、现场采集、模型等上游环节的抖动。

问诊智能体被替身固定（否则问题内容与进度会随模型/候选池变化），但被测的 `interrupt`、
`apply_answers`、预算记账、`route_after_interview`、`route_after_gate` 全是真实实现。

覆盖点：

1. 问诊中断载荷含 `questions` / `stage_hint` / `progress`，恢复后槽位落库、轮次 +1；
2. 预算不因恢复而重置：两次"中断-恢复"后 `interview_rounds == 2`；累计到 4 轮后转人工；
3. 场景一：丢弃图实例、用同一 `thread_id` 重建后仍能恢复，已答槽位不再被重复提问；
4. 场景三：`close_checkpointer()` 后 `get_checkpointer()` 重建，同一 `thread_id` 仍可恢复；
5. 闸门确认中断：degrade 恢复 `continue` / `abort` 都到 `handoff`（不是 `followup`）；
6. 纯函数：`apply_answers` 的键与状态归一、`progress_of` 的口径；
7. `aget_state(config).next` 在中断中非空、在 `END` 后为空。
"""

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.blackboard import apply_answers, progress_of
from app.agent.budget import ConsultBudget
from app.agent.checkpointer import close_checkpointer, get_checkpointer
from app.agent.contracts import (
    FollowupPlan,
    InterviewBatch,
    InterviewQuestion,
    PesticideItem,
    Prescription,
    PrescriptionAction,
    dump_contract,
)
from app.agent.graph import factories, gate_graph
from app.agent.graph.routes import ROUTE_AFTER_GATE, ROUTE_AFTER_INTERVIEW, route_after_gate, route_after_interview
from app.agent.state import ConsultStage, ConsultationState, ProcessId
from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.contracts import GateResult, GateViolation
from app.process_config.loader import get_process_config
from app.process_config.redlines import LoadedRedlines, Redline
from app.process_config.rule_ids import GateRuleId

# 每轮问题数上限（产品说明书 4.2.2）。无会诊流程从共用问诊路由里剔除 panel 分支。
_MAX_QUESTIONS = 2
_INTERVIEW_ROUTES = {key: value for key, value in ROUTE_AFTER_INTERVIEW.items() if key != "panel"}


# ---- 问诊：替身智能体 + 最小图 ----


class _FakeInterviewer:
    """固定问某个槽位的问题，绕开模型与候选池，让断言只看中断与预算行为。"""

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

    `make_interview_node` 在**建造节点时**读模块全局，故替换必须在 `_interview_app` 之前。
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
        "session_id": "sess-interrupt",
        "redlines_loaded": True,
        "required_slots": [],
        "slots": {},
        "budget": ConsultBudget().to_state(),
        "round": 0,
    }
    base.update(overrides)
    return base


def _interrupt_payload(result: dict) -> dict:
    """从 ainvoke 结果里取出中断载荷（`__interrupt__[0].value`）。"""
    interrupts = result.get("__interrupt__")
    assert interrupts, f"预期出现中断，实际结果：{result}"
    return interrupts[0].value


# ---- 1. 问诊中断与恢复 ----


async def test_interview_interrupts_then_resumes_with_answer(monkeypatch) -> None:
    """首轮在 interview 中断，载荷含 questions/stage_hint/progress；恢复后槽位落库、轮次 +1。"""
    _patch_interviewer(monkeypatch, "symptom")
    app = _interview_app(InMemorySaver())
    config = {"configurable": {"thread_id": "t-int-basic"}}

    first = await app.ainvoke(_interview_state(required_slots=["symptom"]), config)

    payload = _interrupt_payload(first)
    assert {q["slot_key"] for q in payload["questions"]} == {"symptom"}
    assert len(payload["questions"]) <= _MAX_QUESTIONS
    assert payload["stage_hint"]
    assert payload["progress"] == {"current": 1, "total": 4}
    assert (await app.aget_state(config)).next  # 中断中：next 指向待执行节点

    second = await app.ainvoke(Command(resume={"answers": [{"slotKey": "symptom", "value": "叶片发黄"}]}), config)

    assert second["slots"]["symptom"].value == "叶片发黄"
    assert second["slots"]["symptom"].status == "filled"
    assert second["budget"]["interview_rounds"] == 1
    assert second["stage"] == ConsultStage.VERIFY
    assert (await app.aget_state(config)).next == ()  # 走到 END


async def test_interview_budget_not_reset_across_two_resumes(monkeypatch) -> None:
    """两次"中断-恢复"后轮次为 2 而非 1——恢复时不得重置预算。"""
    # 替身只问 symptom，required 永远差一项，因此可持续中断，便于观察轮次累加。
    _patch_interviewer(monkeypatch, "symptom")
    app = _interview_app(InMemorySaver())
    config = {"configurable": {"thread_id": "t-int-budget2"}}
    state = _interview_state(required_slots=["symptom", "unreachable"])

    await app.ainvoke(state, config)  # 第 1 次中断
    after_one = await app.ainvoke(Command(resume={"answers": [{"slotKey": "symptom", "value": "x"}]}), config)
    assert after_one["budget"]["interview_rounds"] == 1
    assert _interrupt_payload(after_one)["progress"]["current"] == 2  # 仍在问诊，出现第 2 次中断

    after_two = await app.ainvoke(Command(resume={"answers": []}), config)
    assert after_two["budget"]["interview_rounds"] == 2  # 不是 1：预算跟着检查点走
    assert _interrupt_payload(after_two)["progress"]["current"] == 3


async def test_interview_budget_exhausted_goes_handoff(monkeypatch) -> None:
    """连续四轮仍未收敛时路由转人工（子图出口 __end__），不无限追问。"""
    _patch_interviewer(monkeypatch, "symptom")
    app = _interview_app(InMemorySaver())
    config = {"configurable": {"thread_id": "t-int-exhaust"}}
    state = _interview_state(required_slots=["symptom", "unreachable"])

    await app.ainvoke(state, config)
    result: dict = {}
    for _ in range(4):
        result = await app.ainvoke(Command(resume={"answers": []}), config)

    assert result["budget"]["interview_rounds"] == 4
    assert (await app.aget_state(config)).next == ()  # 未收敛出口：不再中断


# ---- 2. 场景一：丢弃图实例后用同一 thread_id 恢复 ----


async def test_resume_after_discarding_graph_instance(monkeypatch) -> None:
    """检查点在共享 saver 上，重建图实例后同一 thread_id 仍能续问且已答槽位保留。"""
    _patch_interviewer(monkeypatch, "symptom")
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t-int-rebuild"}}
    state = _interview_state(required_slots=["symptom", "other"])

    app1 = _interview_app(saver)
    await app1.ainvoke(state, config)
    answered = await app1.ainvoke(Command(resume={"answers": [{"slotKey": "symptom", "value": "x"}]}), config)
    assert answered["slots"]["symptom"].value == "x"

    # 丢弃图实例，用同一 saver 重建（等价于进程重启但状态仍在）
    app2 = _interview_app(saver)
    resumed = await app2.ainvoke(Command(resume={"answers": [{"slotKey": "other", "value": "y"}]}), config)

    assert resumed["slots"]["symptom"].value == "x"  # 已答槽位未被清空
    assert resumed["slots"]["other"].value == "y"
    assert resumed["budget"]["interview_rounds"] == 2
    assert resumed["stage"] == ConsultStage.VERIFY


# ---- 3. 场景三：检查点器重建后仍可恢复（需真实 PostgreSQL） ----


async def test_resume_after_reopening_postgres_checkpointer(monkeypatch, requires_postgres) -> None:
    """关闭并重建检查点器后，同一 thread_id 的进度不丢（PostgreSQL 持久化）。"""
    _patch_interviewer(monkeypatch, "symptom")
    config = {"configurable": {"thread_id": "t-int-pg-reopen"}}
    state = _interview_state(required_slots=["symptom"])

    saver = await get_checkpointer()
    app1 = _interview_app(saver)
    first = await app1.ainvoke(state, config)
    assert _interrupt_payload(first)["progress"] == {"current": 1, "total": 4}
    await close_checkpointer()

    saver2 = await get_checkpointer()
    app2 = _interview_app(saver2)
    snapshot = await app2.aget_state(config)
    assert snapshot.next  # 中断仍在，没有被当成新会话

    resumed = await app2.ainvoke(Command(resume={"answers": [{"slotKey": "symptom", "value": "x"}]}), config)
    assert resumed["slots"]["symptom"].value == "x"
    assert resumed["budget"]["interview_rounds"] == 1


# ---- 4. 闸门确认中断 ----


class _FakeKnowledge:
    def get_entry(self, knowledge_id: str):
        return None

    def is_approved(self, knowledge_id: str) -> bool:
        return False

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ()


class _StubGateService:
    """固定返回预设 `GateResult`，隔离引擎与审计（另由 T2-45/T2-46 覆盖）。"""

    def __init__(self, result: GateResult) -> None:
        self._result = result

    async def review(self, draft: object, ctx: object) -> GateResult:
        return self._result


def _prescription_dict() -> dict:
    prescription = Prescription(
        diagnosis_summary="水稻稻飞虱若虫危害",
        actions=[
            PrescriptionAction(
                order=1,
                action="建议喷施吡虫啉",
                timing="晴天傍晚",
                method="叶面均匀喷雾",
                cautions=["避免高温施药"],
                stop_conditions=["出现药害斑立即停用"],
            )
        ],
        safety_interval_days=7,
        max_uses_per_season=2,
        pesticide_items=[
            PesticideItem(
                knowledge_id="K-1001",
                name="吡虫啉",
                dosage_min=15.0,
                dosage_max=20.0,
                safety_interval_days=7,
                max_uses_per_season=2,
            )
        ],
        soil_level_actions=[],
        followup_plan=FollowupPlan(tasks=[]),
        facts_provenance={
            "safety_interval_days": "knowledge:K-1001",
            "max_uses_per_season": "knowledge:K-1001",
            "pesticide_items[0].dosage_min": "knowledge:K-1001:field:dosage_min",
            "pesticide_items[0].dosage_max": "knowledge:K-1001:field:dosage_max",
            "pesticide_items[0].safety_interval_days": "knowledge:K-1001",
            "pesticide_items[0].max_uses_per_season": "knowledge:K-1001",
        },
        requires_tech_station=False,
        detail_terms={},
    )
    return dump_contract(prescription)


def _loaded_redlines_dict() -> dict:
    return LoadedRedlines(
        process_id=ProcessId.RICE,
        version=1,
        redlines=(
            Redline(
                rule_ids=(GateRuleId.REDLINE,),
                id="RICE-01",
                desc="禁用清单",
                severity="block",
                payload={"forbidden": ["某某禁用农药"]},
            ),
        ),
        loaded=True,
    ).model_dump(mode="json")


def _gate_state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "sess-gate",
        "user_id": "user-0001",
        "process_id": ProcessId.RICE,
        "config_version": 1,
        "prescription": _prescription_dict(),
        "prescription_text": "建议在傍晚喷施吡虫啉，出现药害立即停用。",
        "redlines": _loaded_redlines_dict(),
        "slots": {},
        "prescription_history": [],
        "region_code": None,
        "messages": [{"role": "user", "content": "水稻叶子上有虫"}],
    }
    base.update(overrides)
    return base


def _gate_result(decision: str) -> GateResult:
    return GateResult(
        passed=decision == "allow",
        violations=(
            [GateViolation(rule_id="R-WHITELIST", severity="block", message="不在已核准库", field="pesticide_items")]
            if decision == "block"
            else []
        ),
        checked_rules=["R-REDLINE-LOAD"],
        process_redlines_loaded=True,
        decision=decision,
        degrade_output=DEGRADE_OUTPUT_TEXT if decision == "degrade" else None,
    )


async def _followup_stub(state: ConsultationState) -> dict:
    return {"stage": ConsultStage.FOLLOWUP}


async def _handoff_stub(state: ConsultationState) -> dict:
    return {"stage": ConsultStage.HANDOFF, "reply": "这个方案不建议自己动手，建议先问农技站"}


def _gate_app(monkeypatch, decision: str, checkpointer):
    """最小闸门图：gate（真实子图）→ route_after_gate → followup / handoff。

    `block` 分支会触发复核工单，`GateService` 既已用桩替换，工单入队也一并桩掉：本文件
    只验证中断与路由，工单落库由 T2-46 单独覆盖，不在此牵动数据库。
    """
    monkeypatch.setattr(gate_graph, "GateService", lambda *a, **k: _StubGateService(_gate_result(decision)))

    async def _fake_ticket(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(gate_graph, "enqueue_review_ticket", _fake_ticket)

    graph = StateGraph(ConsultationState)
    graph.add_node("gate", gate_graph.build_gate_subgraph(_FakeKnowledge()))
    graph.add_node("followup", _followup_stub)
    graph.add_node("handoff", _handoff_stub)
    graph.add_edge(START, "gate")
    graph.add_conditional_edges("gate", route_after_gate, ROUTE_AFTER_GATE)
    graph.add_edge("followup", END)
    graph.add_edge("handoff", END)
    return graph.compile(checkpointer=checkpointer)


async def test_gate_degrade_interrupts_and_continue_goes_handoff(monkeypatch) -> None:
    """degrade 在 gate 中断；恢复 `continue` 后走 handoff（不是放行 followup）。"""
    app = _gate_app(monkeypatch, "degrade", InMemorySaver())
    config = {"configurable": {"thread_id": "t-gate-degrade"}}

    first = await app.ainvoke(_gate_state(), config)

    payload = _interrupt_payload(first)
    assert payload["kind"] == "gate_confirm"
    assert payload["decision"] == "degrade"
    assert payload["options"] == ["continue", "abort"]
    assert (await app.aget_state(config)).next

    resumed = await app.ainvoke(Command(resume="continue"), config)

    assert resumed["gate_confirmed"] is True
    assert resumed["stage"] == ConsultStage.HANDOFF
    assert resumed["stage"] != ConsultStage.FOLLOWUP
    assert (await app.aget_state(config)).next == ()


async def test_gate_degrade_abort_goes_handoff_with_abort_reply(monkeypatch) -> None:
    """恢复 `abort` 同样到 handoff，且处方被抑制。"""
    app = _gate_app(monkeypatch, "degrade", InMemorySaver())
    config = {"configurable": {"thread_id": "t-gate-abort"}}

    await app.ainvoke(_gate_state(), config)
    resumed = await app.ainvoke(Command(resume="abort"), config)

    assert resumed["gate_confirmed"] is False
    assert resumed["stage"] == ConsultStage.HANDOFF
    assert resumed["prescription_suppressed"] is True
    assert "不建议" in resumed["reply"]


async def test_gate_block_interrupts_and_never_outputs_prescription(monkeypatch) -> None:
    """block 在 gate 中断，恢复后同样转人工，处方始终被抑制、不被放行。

    节点级 `stage=BLOCKED` 由 `test_gate_subgraph.py` 的单节点用例固定；此处跑的是
    `route_after_gate`——block 一律走 `handoff`（fail-closed），故端到端看到的是
    `HANDOFF` 且处方被抑制。
    """
    app = _gate_app(monkeypatch, "block", InMemorySaver())
    config = {"configurable": {"thread_id": "t-gate-block"}}

    first = await app.ainvoke(_gate_state(), config)
    assert _interrupt_payload(first)["decision"] == "block"

    resumed = await app.ainvoke(Command(resume="continue"), config)

    assert resumed["stage"] == ConsultStage.HANDOFF
    assert resumed["prescription_suppressed"] is True
    assert "吡虫啉" not in (resumed.get("prescription_text") or "")
    assert (await app.aget_state(config)).next == ()


# ---- 5. 纯函数辅助 ----


def test_apply_answers_normalizes_keys_and_status() -> None:
    """答案键接受 slotKey / slot_key 两种写法，unknown 落成不带值的合法答案。"""
    slots = apply_answers(
        {},
        {
            "answers": [
                {"slotKey": "symptom", "value": "发黄"},
                {"slot_key": "stage", "value": "分蘖期"},
                {"slotKey": "water", "status": "unknown"},
                {"slotKey": "blank", "value": "  "},
            ]
        },
    )

    assert slots["symptom"].value == "发黄"
    assert slots["stage"].value == "分蘖期"
    assert slots["water"].status == "unknown"
    assert "blank" not in slots  # 空值 filled 被丢弃，不写成非法状态


def test_apply_answers_ignores_non_mapping_and_keeps_existing() -> None:
    """脏数据不打断问诊，且已有槽位在合并后保留。"""
    existing = apply_answers({}, [{"slotKey": "symptom", "value": "发黄"}])

    merged = apply_answers(existing, ["not-a-mapping", 42])

    assert merged["symptom"].value == "发黄"


def test_progress_of_matches_interview_stage_hint() -> None:
    """进度口径：当前轮为已完成轮次 + 1，上限取预算里的 max_interview_rounds。

    与 `InterviewerAgent.compute_stage_hint` 同源：轮次计数器统一用
    `budget.interview_rounds`（T2-31 起取代旧的 `round`）。
    """
    state = {"budget": {"max_interview_rounds": 4, "interview_rounds": 2}}
    assert progress_of(state) == {"current": 3, "total": 4}


def test_progress_of_clamps_to_total() -> None:
    """轮次用尽后的补问不出现越界进度。"""
    state = {"budget": {"max_interview_rounds": 4, "interview_rounds": 9}}
    assert progress_of(state) == {"current": 4, "total": 4}


def test_progress_of_defaults_without_budget() -> None:
    assert progress_of({}) == {"current": 1, "total": ConsultBudget().max_interview_rounds}
