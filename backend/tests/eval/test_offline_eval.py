"""离线评测集断言骨架（T6-01，技术方案 14.1）。

本文件是"评测集怎么被判分"的**唯一实现**：七条流程各一个 `parametrize`，谁也不合并
——合并后一道题失败只能看到"总表"，无法定位到是哪条流程退化。断言骨架逐条对齐 14.1：

| 断言 | 含义 |
| --- | --- |
| `result.process_id == case.expect.expect_process` | 受理分流正确（对应 2.2 缺口一） |
| `must_ask_slots ⊆ result.asked_slots` | 该问到的都问到了 |
| `result.conclusion_key ∈ allowed_conclusions` | 结论落在允许集合内 |
| `expect_gate_rules ⊆ result.gate.rules_hit` | 期望的闸门规则确实被触发 |
| `forbidden_phrases` 不出现在 `final_text` | 安全红线文案未越界 |
| `result.sources ⊆ allowed_sources` | 引用来源未越界 |

**注意用例骨架里的字段名**：技术方案 14.1 断言骨架写作 `case.expect.process`，而
`EvalExpectation` 定义的是 `expect_process`（本项以字段定义为准，方案骨架是笔误）。
刻意不做别名兼容，避免两套写法并存掩盖契约漂移。

`cases/` 目前为空（内容由 T6-02 填充），因此七个 `parametrize` 都是空表、不产生用例；
下面的端到端冒烟用例用一道内联题验证运行器本身可用。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import gate_graph
from app.agent.graph import main as main_graph
from app.gate.contracts import GateResult
from app.agent.state import ProcessId
from tests.eval.runner import EvalRunner
from tests.eval.schema import EvalCase, EvalResult, load_cases

pytestmark = pytest.mark.eval


# ---- 图装配：隔离 checkpointer 与闸门落库，保留真实编排 ----


class _StubGateService:
    """闸门替身：返回一个恒 `allow` 的判定，隔离引擎与审计落库。

    评测骨架验证的是**编排与契约**（14.1），闸门规则本身由 T6-03 的单测覆盖；若在这里
    跑真实引擎，评测会同时依赖知识库与数据库，失去"可离线重放"的前提。
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def review(self, draft: object, ctx: object) -> GateResult:
        return GateResult(passed=True, checked_rules=["R-WHITELIST"], decision="allow")


async def _in_memory_checkpointer() -> InMemorySaver:
    return InMemorySaver()


@pytest.fixture
async def eval_graph(monkeypatch: pytest.MonkeyPatch):
    """编译一张"只把外部副作用换成替身"的整图，供评测端到端跑。

    替换两处：checkpointer（内存，避免依赖 PostgreSQL）与闸门服务（避免审计写库）。
    编排层（`main.py`、`routes.py`、七条子图）全用真实实现——评测要验证的正是它们。
    """
    monkeypatch.setattr(main_graph, "get_checkpointer", _in_memory_checkpointer)
    monkeypatch.setattr(gate_graph, "GateService", _StubGateService)
    return await main_graph.build_graph()


def _assert_case(result: EvalResult, case: EvalCase) -> None:
    """按 14.1 的断言骨架逐条判定。失败信息带 `case_id`，便于直接定位。"""
    expect = case.expect
    assert result.process_id == expect.expect_process, f"{case.case_id}: 分流错误 {result.process_id}"
    assert set(expect.must_ask_slots) <= set(result.asked_slots), f"{case.case_id}: 漏问 {expect.must_ask_slots}"
    assert result.conclusion_key in expect.allowed_conclusions, f"{case.case_id}: 结论 {result.conclusion_key} 越界"
    assert set(expect.expect_gate_rules) <= set(result.gate.rules_hit), (
        f"{case.case_id}: 闸门规则未命中 {expect.expect_gate_rules} ⊄ {result.gate.rules_hit}"
    )
    for phrase in expect.forbidden_phrases:
        assert phrase not in result.final_text, f"{case.case_id}: 出现禁用表述 {phrase}"
    assert set(result.sources) <= set(expect.allowed_sources), f"{case.case_id}: 来源 {result.sources} 越界"


# ---- 七条流程各一个评测集（内容由 T6-02 填充，此处不合并） ----

_RUNNER_KWARGS = {"knowledge_version": "test"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.RICE), ids=lambda c: c.case_id)
async def test_offline_eval_rice(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.TEA), ids=lambda c: c.case_id)
async def test_offline_eval_tea(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.RAPESEED), ids=lambda c: c.case_id)
async def test_offline_eval_rapeseed(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.CHILI), ids=lambda c: c.case_id)
async def test_offline_eval_chili(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.WEED), ids=lambda c: c.case_id)
async def test_offline_eval_weed(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.PIG), ids=lambda c: c.case_id)
async def test_offline_eval_pig(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.GENERAL), ids=lambda c: c.case_id)
async def test_offline_eval_general(eval_graph, case: EvalCase) -> None:
    _assert_case(await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case), case)


# ---- 运行器端到端冒烟（不依赖 cases/ 内容） ----


def _smoke_case(case_id: str) -> EvalCase:
    return EvalCase.model_validate(
        {
            "case_id": case_id,
            "process_id": "rice",
            "scene": "冒烟",
            "inputs": {"message": "我家水稻叶子发黄"},
            "expect": {
                "expect_process": "rice",
                "allowed_conclusions": ["mock 模式下的示例结论"],
            },
        }
    )


@pytest.mark.asyncio
async def test_runner_executes_end_to_end(eval_graph) -> None:
    """整图能跑完并抽出结果：受理 → 分流 → 子图 → （问诊中断恢复）→ 出口。"""
    result = await EvalRunner(eval_graph, knowledge_version="test").run(_smoke_case("smoke-e2e"))

    assert result.process_id is ProcessId.RICE
    assert result.node_path[0:2] == ["intake", "triage"]
    assert any(name == "subgraph_rice" for name in result.node_path)


@pytest.mark.asyncio
async def test_runner_node_path_is_deterministic(eval_graph) -> None:
    """同一输入连续两次评测，`node_path` 完全一致（编排可重放）。"""
    runner = EvalRunner(eval_graph, knowledge_version="test")
    first = await runner.run(_smoke_case("smoke-det-1"))
    second = await runner.run(_smoke_case("smoke-det-2"))

    assert first.node_path == second.node_path


@pytest.mark.asyncio
async def test_runner_records_model_mode(eval_graph) -> None:
    """报告字段 `model_mode` 在 mock 模式下取 `mock`（区分回归来源）。"""
    assert EvalRunner(eval_graph, knowledge_version="test").model_mode == "mock"


# ---- 分集权重与汇总（T6-02） ----


def test_weights_read_from_file_not_hardcoded() -> None:
    """权重从 `weights.toml` 读取：`general=1.5`、`rapeseed=0.5`（不是写死的常量）。"""
    from tests.eval.runner import load_weights

    weights = load_weights()
    assert weights["general"] == pytest.approx(1.5)
    assert weights["rapeseed"] == pytest.approx(0.5)


def test_weighted_consistency_responds_to_weight_change() -> None:
    """改 `general` 权重会改变加权一致率——证明运行器真的用了权重而非忽略。"""
    from tests.eval.runner import weighted_consistency

    per_process = {"general": (3, 6), "tea": (10, 10)}  # general 一半失败
    low = weighted_consistency(per_process, {"general": 1.0, "tea": 1.0})
    high = weighted_consistency(per_process, {"general": 1.5, "tea": 1.0})
    assert high < low, "提高失分流程的权重应降低加权一致率"


def test_build_report_has_per_process_breakdown() -> None:
    """报告按流程分集给出通过数与一致率，并给出加权后的整体一致率。"""
    from tests.eval.report import build_report

    report = build_report(
        {"tea": (9, 10), "general": (4, 6)},
        knowledge_version="2026-09-01",
        model_mode="mock",
    )
    assert report["processes"]["tea"]["consistency"] == pytest.approx(0.9)
    assert report["knowledge_version"] == "2026-09-01"
    assert report["model_mode"] == "mock"
    assert "overall" in report
