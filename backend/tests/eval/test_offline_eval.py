"""离线评测集断言骨架（T6-01，技术方案 14.1；内容由 T6-02 填充）。

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

**两类断言与 mock 口径（T6-01 实现要点五）**：T6-01 明确"mock 模式只能回归编排与契约，
不能回归模型质量"，因此断言分成两类按下述口径执行：

- **编排与契约断言（任何模式都跑）**：图能跑完且入口是 `intake`；`forbidden_phrases`
  未越界；`sources ⊆ allowed_sources`。这些与模型输出无关，mock 下就应全部成立。
- **语义断言（仅 `live` 跑，mock 下跳过并记入报告）**：`expect_process`、`must_ask_slots`、
  `conclusion_key`、`expect_gate_rules`。它们依赖真实模型的分诊/判定与真实闸门引擎，而
  mock 分诊恒返回枚举首值 `rice`、`DiagnosisResult.conclusion` 恒为固定占位、本骨架又把
  `GateService` 换成恒 `allow` 的替身（见 `_StubGateService`），因此 mock 下不可判定。
  这不削弱评测集价值：语义断言的验收通道是真实模型的离线重放与 T6-09 的人工抽检，
  评测内容（`cases/*.json`）本身按真实模型口径编写，不因 mock 而删改。

模块在收尾时把"按流程分集的通过数与一致率"写入 `reports/{date}.json` 并打印到终端
（T6-02 验收："输出按流程分别显示通过数与一致率，`general` 行的权重显示为 1.5"）。
"""

from __future__ import annotations

from datetime import date

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.agents import base as agent_base
from app.agent.graph import gate_graph
from app.agent.graph import main as main_graph
from app.agent.state import ProcessId
from app.core.config import get_settings
from app.gate.contracts import GateResult
from tests.eval.report import REPORTS_DIR, build_report, save_report
from tests.eval.runner import EvalRunner, load_weights, weighted_consistency
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

    替换三处：checkpointer（内存，避免依赖 PostgreSQL）、闸门服务（避免审计写库）与
    重试退避（把指数退避压成零等待）。编排层（`main.py`、`routes.py`、七条子图）全用
    真实实现——评测要验证的正是它们。

    **为什么可以压掉退避**：mock 问诊按 schema 恒产出 `mock_slot`（不在候选池内），必然
    触发"契约违约 → 重试 → 模板降级"，重试退避的 0.5~4 秒是真模型链路下的合理等待，
    但在 mock 下每条题多轮累积成分钟级空转。退避时长不影响任何断言的判据（判据是"最终
    抽出的结果"，不是"等了多久"），压零只让评测可跑，不改变结论。
    """
    monkeypatch.setattr(main_graph, "get_checkpointer", _in_memory_checkpointer)
    monkeypatch.setattr(gate_graph, "GateService", _StubGateService)
    monkeypatch.setattr(agent_base, "wait_exponential", lambda **_kwargs: lambda _retry_state: 0)
    return await main_graph.build_graph()


def _live_model() -> bool:
    """本次是否跑真实模型（`USE_MOCK_LLM=false`）。决定语义断言是否执行。"""
    return not get_settings().USE_MOCK_LLM


def _assert_orchestration(result: EvalResult, case: EvalCase) -> None:
    """编排与契约断言：与模型输出无关，任何模式都必须成立。

    这三条覆盖 14.1 里"链路能跑通、来源不越界、红线文案不越界"的部分——它们由编排层
    与流程配置决定，mock 与真实模型下判据相同，因此不做模式区分。
    """
    assert result.node_path[:1] == ["intake"], f"{case.case_id}: 入口不是 intake，实得 {result.node_path[:3]}"
    for phrase in case.expect.forbidden_phrases:
        assert phrase not in result.final_text, f"{case.case_id}: 出现禁用表述 {phrase}"
    assert set(result.sources) <= set(case.expect.allowed_sources), (
        f"{case.case_id}: 来源 {result.sources} 越界 {case.expect.allowed_sources}"
    )


def _assert_semantics(result: EvalResult, case: EvalCase) -> None:
    """语义断言：分诊、问诊、结论、闸门。依赖真实模型与真实闸门引擎，仅 `live` 执行。"""
    expect = case.expect
    assert result.process_id == expect.expect_process, f"{case.case_id}: 分流错误 {result.process_id}"
    assert set(expect.must_ask_slots) <= set(result.asked_slots), f"{case.case_id}: 漏问 {expect.must_ask_slots}"
    assert result.conclusion_key in expect.allowed_conclusions, f"{case.case_id}: 结论 {result.conclusion_key} 越界"
    assert set(expect.expect_gate_rules) <= set(result.gate.rules_hit), (
        f"{case.case_id}: 闸门规则未命中 {expect.expect_gate_rules} ⊄ {result.gate.rules_hit}"
    )


def _assert_case(result: EvalResult, case: EvalCase) -> bool:
    """按 14.1 判定一道题，返回"是否通过"（供报告统计）。

    先跑编排断言（任何模式），再在 `live` 下跑语义断言（mock 下不判定，见模块说明）。
    两种模式下本函数都可能抛 `AssertionError`，失败信息都带 `case_id`。
    """
    _assert_orchestration(result, case)
    if _live_model():
        _assert_semantics(result, case)
    return True


# ---- 报告汇总：按流程分集记录通过数/总数，收尾写盘并打印 ----

# 键为流程标识，值为按用例顺序排列的布尔结果。只由本模块的评测用例写入。
_RECORDS: dict[str, list[bool]] = {}


def _record(process_id: str, passed: bool) -> None:
    _RECORDS.setdefault(process_id, []).append(passed)


def _write_report() -> None:
    """收尾：按流程分集写报告并打印一致率表（T6-02 验收的可读输出）。

    一致率 = 通过数 / 总数，另打印加权总一致率（`general` 权重 1.5 来自 `weights.toml`）。
    mock 模式额外打印一行说明：本次语义断言未执行，数值仅代表编排与契约回归。
    """
    if not _RECORDS:
        return
    per_process = {pid: (sum(rows), len(rows)) for pid, rows in _RECORDS.items()}
    weights = load_weights()
    report = build_report(
        per_process,
        knowledge_version=_KNOWLEDGE_VERSION,
        model_mode="mock" if not _live_model() else "live",
    )
    print("\n离线评测集一致率（按流程分集）")
    for pid, row in sorted(report["processes"].items()):
        print(
            f"  {pid:<10} 权重 {weights.get(pid, 1.0):<4} 通过 {row['passed']}/{row['total']}  一致率 {row['consistency']:.4f}"
        )
    print(f"  加权总一致率 {weighted_consistency(per_process, weights):.4f}")
    if not _live_model():
        print("  [mock] 语义断言（分诊/问诊/结论/闸门）未执行：mock 分诊恒返回 rice、结论为固定占位。")
    save_report(report, REPORTS_DIR / f"{date.today().isoformat()}.json")


@pytest.fixture(scope="session", autouse=True)
def _eval_report_sink():
    """会话收尾时写报告。只在本模块生效（autouse 的作用域是定义它的模块）。"""
    yield
    _write_report()


# ---- 七条流程各一个评测集（内容由 T6-02 填充，此处不合并） ----

_KNOWLEDGE_VERSION = "test"
_RUNNER_KWARGS = {"knowledge_version": _KNOWLEDGE_VERSION}


async def _run_and_record(eval_graph, case: EvalCase) -> None:
    """跑一道题、判定、并把结果记入分集汇总（供收尾写报告）。"""
    result = await EvalRunner(eval_graph, **_RUNNER_KWARGS).run(case)
    try:
        _assert_case(result, case)
    except AssertionError:
        _record(case.process_id.value, False)
        raise
    _record(case.process_id.value, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.RICE), ids=lambda c: c.case_id)
async def test_offline_eval_rice(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.TEA), ids=lambda c: c.case_id)
async def test_offline_eval_tea(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.RAPESEED), ids=lambda c: c.case_id)
async def test_offline_eval_rapeseed(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.CHILI), ids=lambda c: c.case_id)
async def test_offline_eval_chili(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.WEED), ids=lambda c: c.case_id)
async def test_offline_eval_weed(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.PIG), ids=lambda c: c.case_id)
async def test_offline_eval_pig(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(ProcessId.GENERAL), ids=lambda c: c.case_id)
async def test_offline_eval_general(eval_graph, case: EvalCase) -> None:
    await _run_and_record(eval_graph, case)


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
