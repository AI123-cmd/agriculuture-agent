"""T2-20：受限并行会诊的聚合裁定、候选域约束与单链兜底。

用例按五组组织：

1. 硬判据裁定：唯一视角命中决定性特征时 `resolution="decisive_feature"`，多视角一致命中
   同一候选时同样命中该分支；
2. 排除项裁定：被排除视角排除后仅剩一个候选时 `resolution="elimination"`；无判据且多候选时
   `resolution="unresolved"` 且 `force_uncertain=True`；空意见列表不抛异常；
3. 排除的效力来源：`reduce_exclusions` 忽略不在任何 `confusable_items` 候选集里的排除项；
4. 纯规则与可复现：`aggregate_panel` 的签名无网关会话，同一输入连续调用 100 次结果一致；
5. 面板运行器：未启用流程不发任何调用，两路失败时返回一路并记录 warning，超时与解析失败
   都按单链兜底处理。

网关是替身（面板在网关面前是被调用方），流程配置取真实的辣椒、茶与油菜流程。
"""

import inspect
import json
from typing import Any, cast

import pytest
import respx
from pydantic import ValidationError

from app.agent.agents.base import AgentContext
from app.agent.agents.panel import (
    PERSPECTIVES,
    PERSPECTIVE_FOCUS,
    PanelAggregate,
    PanelOpinion,
    PanelRunner,
    aggregate_panel,
    all_candidates,
    build_panel_prompt,
    reduce_exclusions,
)
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.llm import ModelGateway
from app.llm.providers.base import ModelResponse
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_MESSAGE = "我家辣椒突然蔫了，叶子还是绿的"
# 辣椒流程的易混淆项候选。
_BACTERIAL_WILT = "青枯病"
_WILT = "枯萎病"
# 配置里没有判据的排除项。它必须被忽略：没有配置支撑的排除只是模型的随口判断。
_UNCONFIGURED = "感冒"


def _opinion(**overrides: Any) -> PanelOpinion:
    data: dict[str, Any] = {
        "perspective": "symptom",
        "candidate": _BACTERIAL_WILT,
        "support": ["叶片保绿而急速萎蔫"],
        "excluded_candidates": [],
        "decisive_feature_hit": None,
        "confidence": "preliminary",
    }
    data.update(overrides)
    return PanelOpinion.model_validate(data)


def _ok(perspective: str, **overrides: Any) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象（适配器负责校验），不是裸字典。"""
    payload = _opinion(perspective=perspective, **overrides).model_dump()
    return ModelResponse(
        content=json.dumps(payload, ensure_ascii=False),
        parsed=PanelOpinion.model_validate(payload),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


def _parse_failure(perspective: str) -> ModelResponse:
    """解析失败：`parsed` 为空且带错误说明。"""
    return ModelResponse(
        content="（模型输出不是合法 JSON）",
        parsed=None,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1,
        error="parse_error",
    )


class _FakeGateway:
    """回放预设结果的网关替身。按视角名（提示词里的关注点）选择结果。

    面板三路并发，调用顺序不确定，因此不能按"第几次调用"回放——这里按提示词内容里出现的
    视角关注点识别是哪一路。
    """

    def __init__(self, by_perspective: dict[str, ModelResponse | Exception]) -> None:
        self._by_perspective = by_perspective
        self.calls: list[tuple[object, list[dict], object]] = []

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.calls.append((task_type, list(messages), schema))
        perspective = self._perspective_of(messages)
        result = self._by_perspective.get(perspective, RuntimeError(f"未预设视角 {perspective} 的结果"))
        if isinstance(result, Exception):
            raise result
        return result

    @staticmethod
    def _perspective_of(messages: list[dict]) -> str:
        system = messages[0]["content"]
        for name, focus in PERSPECTIVE_FOCUS.items():
            if focus in system:
                return name
        raise AssertionError("提示词里未找到任何视角关注点")


class _LogRecorder:
    """记录结构化日志事件的替身，用于断言"不足两路意见时确实告警"。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.fixture(scope="module")
def chili_config() -> ProcessConfig:
    return get_process_config(ProcessId.CHILI)


@pytest.fixture(scope="module")
def rapeseed_config() -> ProcessConfig:
    return get_process_config(ProcessId.RAPESEED)


@pytest.fixture(scope="module")
def weed_config() -> ProcessConfig:
    return get_process_config(ProcessId.WEED)


def _ctx(config: ProcessConfig) -> AgentContext:
    return AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.PANEL,
        trace_id="t-1",
        region_code="330102",
    )


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _MESSAGE}],
    }
    state.update(overrides)
    return state


def _runner(
    by_perspective: dict[str, ModelResponse | Exception], *, timeout: float = 20.0
) -> tuple[PanelRunner, _FakeGateway]:
    gateway = _FakeGateway(by_perspective)
    return PanelRunner(cast(ModelGateway, gateway), timeout=timeout), gateway


# ---- 第一组：硬判据裁定 ----


def test_single_decisive_feature_wins(chili_config: ProcessConfig) -> None:
    """唯一视角命中决定性特征时取该视角，排除项按模型给出的原样汇总。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT, decisive_feature_hit="挤压切口渗出乳白色菌脓"),
        _opinion(perspective="environment", candidate=_WILT),
        _opinion(perspective="exclusion", candidate=None, excluded_candidates=[_WILT]),
    ]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "decisive_feature"
    assert aggregate.force_uncertain is False
    assert aggregate.winner is not None
    assert aggregate.winner.perspective == "symptom"
    assert aggregate.candidates == [_BACTERIAL_WILT]
    assert "挤压切口渗出乳白色菌脓" in aggregate.rationale
    assert aggregate.excluded == [_WILT]


def test_consistent_decisive_candidates_hit_same_branch(chili_config: ProcessConfig) -> None:
    """两个视角都命中决定性特征且指向同一候选时落同一分支。

    特征原文不同（各自引用了不同判据）故 `len(decisive) == 2`，走的是"候选一致"那条。
    """
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT, decisive_feature_hit="维管束黄褐色"),
        _opinion(perspective="environment", candidate=_BACTERIAL_WILT, decisive_feature_hit="叶片保绿而急速萎蔫"),
        _opinion(perspective="exclusion", candidate=None, excluded_candidates=[_WILT]),
    ]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "decisive_feature"
    assert aggregate.candidates == [_BACTERIAL_WILT]
    assert "多视角一致" in aggregate.rationale


def test_conflicting_decisive_candidates_fall_through(chili_config: ProcessConfig) -> None:
    """两视角命中决定性特征但指向不同候选时不得择一：硬判据失效，落入后两段。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT, decisive_feature_hit="维管束黄褐色"),
        _opinion(perspective="environment", candidate=_WILT, decisive_feature_hit="无菌脓"),
    ]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution != "decisive_feature"
    assert aggregate.force_uncertain is True


# ---- 第二组：排除项裁定与未收敛 ----


def test_elimination_leaves_single_candidate(chili_config: ProcessConfig) -> None:
    """被排除视角排除后仅剩一个候选：裁定为排除法，且胜出意见来自给出该候选的视角。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT),
        _opinion(perspective="environment", candidate=_WILT),
        _opinion(perspective="exclusion", candidate=None, excluded_candidates=[_WILT]),
    ]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "elimination"
    assert aggregate.force_uncertain is False
    assert aggregate.candidates == [_BACTERIAL_WILT]
    assert aggregate.excluded == [_WILT]
    assert aggregate.winner is not None
    assert aggregate.winner.perspective == "symptom"
    assert _BACTERIAL_WILT in aggregate.rationale


def test_unresolved_forces_uncertain(chili_config: ProcessConfig) -> None:
    """无判据且多候选时标记分歧：不做强行择一，给观察与求助。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT),
        _opinion(perspective="environment", candidate=_WILT),
    ]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "unresolved"
    assert aggregate.force_uncertain is True
    assert aggregate.winner is None
    assert aggregate.candidates == [_BACTERIAL_WILT, _WILT]
    assert "未收敛" in aggregate.rationale


def test_all_candidates_excluded_asks_for_more_evidence(chili_config: ProcessConfig) -> None:
    """全部候选被排除时不返回空结论，而是要求重新收集证据。"""
    opinions = [_opinion(perspective="exclusion", candidate=None, excluded_candidates=[_BACTERIAL_WILT, _WILT])]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "unresolved"
    assert aggregate.force_uncertain is True
    assert aggregate.candidates == []
    assert "重新收集证据" in aggregate.rationale


def test_empty_opinions_returns_uncertain(chili_config: ProcessConfig) -> None:
    """没有任何有效视角意见时不抛异常，直接标记为不确定。"""
    aggregate = aggregate_panel([], chili_config)

    assert aggregate.force_uncertain is True
    assert aggregate.resolution == "unresolved"
    assert aggregate.rationale


def test_exclusion_perspective_without_candidate_is_valid(chili_config: ProcessConfig) -> None:
    """排除视角不给候选是正常的：它只输出排除项，不因此被判为无效意见。"""
    opinions = [_opinion(perspective="exclusion", candidate=None, excluded_candidates=[_WILT])]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "unresolved"
    assert aggregate.excluded == [_WILT]


# ---- 第三组：排除的效力来源 ----


def test_reduce_exclusions_ignores_unconfigured_names(chili_config: ProcessConfig) -> None:
    """没有配置判据支撑的排除被忽略：否则模型可以把正确候选排掉，把结论引向错误方向。"""
    opinions = [_opinion(perspective="exclusion", candidate=None, excluded_candidates=[_WILT, _UNCONFIGURED])]

    assert reduce_exclusions(opinions, chili_config) == [_WILT]


def test_reduce_exclusions_only_accepts_configured_names(chili_config: ProcessConfig) -> None:
    """辣椒流程下能进入 `excluded` 的只有 `confusable_items` 里列出的候选。"""
    configured = {c for pair in chili_config.confusable_items for c in pair.candidates}
    opinions = [
        _opinion(perspective="exclusion", candidate=None, excluded_candidates=[*sorted(configured), _UNCONFIGURED])
    ]

    excluded = reduce_exclusions(opinions, chili_config)

    assert set(excluded) <= configured
    assert _UNCONFIGURED not in excluded


def test_reduce_exclusions_dedupes_and_keeps_order(chili_config: ProcessConfig) -> None:
    """按视角顺序收集、保序去重：`excluded` 会进展示与审计，需要可复现。"""
    opinions = [
        _opinion(perspective="symptom", excluded_candidates=[_BACTERIAL_WILT]),
        _opinion(perspective="environment", excluded_candidates=[_WILT]),
        _opinion(perspective="exclusion", excluded_candidates=[_BACTERIAL_WILT]),
    ]

    assert reduce_exclusions(opinions, chili_config) == [_BACTERIAL_WILT, _WILT]


def test_unconfigured_exclusion_does_not_eliminate(chili_config: ProcessConfig) -> None:
    """用未配置的名称排除不掉任何候选：候选仍在，裁定仍是分歧。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT),
        _opinion(perspective="environment", candidate=_WILT),
        _opinion(perspective="exclusion", candidate=None, excluded_candidates=[_UNCONFIGURED]),
    ]

    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.excluded == []
    assert aggregate.candidates == [_BACTERIAL_WILT, _WILT]
    assert aggregate.resolution == "unresolved"


def test_weed_flow_has_three_way_candidates(weed_config: ProcessConfig) -> None:
    """杂草流程的三候选同组：一对二无从投票，正是"不投票"设计的依据。"""
    opinions = [
        _opinion(perspective="symptom", candidate="稗草"),
        _opinion(perspective="environment", candidate="稻稗"),
        _opinion(perspective="exclusion", candidate=None, excluded_candidates=["千金子"]),
    ]

    aggregate = aggregate_panel(opinions, weed_config)

    assert aggregate.resolution == "unresolved"
    assert aggregate.candidates == ["稗草", "稻稗"]
    assert aggregate.excluded == ["千金子"]


# ---- 第四组：纯规则与可复现 ----


def test_all_candidates_dedupes_in_order(chili_config: ProcessConfig) -> None:
    """两个视角都给同一候选时结果只含一次，且按视角顺序排列。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT),
        _opinion(perspective="environment", candidate=_BACTERIAL_WILT),
        _opinion(perspective="exclusion", candidate=_WILT),
    ]

    assert all_candidates(opinions) == [_BACTERIAL_WILT, _WILT]


def test_all_candidates_skips_none(chili_config: ProcessConfig) -> None:
    """排除视角的 `candidate=None` 不进入候选集合。"""
    opinions = [_opinion(perspective="exclusion", candidate=None), _opinion(perspective="symptom")]

    assert all_candidates(opinions) == [_BACTERIAL_WILT]


def test_aggregate_signature_has_no_io_parameters() -> None:
    """聚合器是纯规则：签名里不得出现网关、数据库会话或任何 IO 依赖。"""
    params = list(inspect.signature(aggregate_panel).parameters)

    assert params == ["opinions", "config"]
    for forbidden in ("gateway", "session", "db"):
        assert forbidden not in params


def test_aggregate_is_deterministic(chili_config: ProcessConfig) -> None:
    """同一输入连续裁定 100 次，各字段完全一致：结果要进审计，必须可复现。"""
    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT),
        _opinion(perspective="environment", candidate=_WILT),
    ]

    first = aggregate_panel(opinions, chili_config)
    for _ in range(99):
        assert aggregate_panel(opinions, chili_config) == first


def test_aggregate_returns_frozen_model(chili_config: ProcessConfig) -> None:
    """裁定结果是不可变对象：它是审计依据，不允许下游改写。"""
    aggregate = aggregate_panel([_opinion()], chili_config)

    assert isinstance(aggregate, PanelAggregate)
    with pytest.raises(ValidationError):
        aggregate.rationale = "被改写"  # type: ignore[misc]


# ---- 第五组：面板运行器 ----


async def test_collect_returns_empty_when_panel_disabled(
    rapeseed_config: ProcessConfig, respx_mock: respx.Router
) -> None:
    """未启用并行会诊的流程不发任何模型调用：这是成本控制的第一条限制。"""
    runner, gateway = _runner({})

    opinions = await runner.collect(rapeseed_config, _state(), _ctx(rapeseed_config))

    assert opinions == []
    assert gateway.calls == []
    assert respx_mock.calls == []


async def test_collect_gathers_three_perspectives(chili_config: ProcessConfig, respx_mock: respx.Router) -> None:
    """启用流程发起三路并行调用，三路都成功时返回三条意见。"""
    runner, gateway = _runner({name: _ok(name) for name in PERSPECTIVES})

    opinions = await runner.collect(chili_config, _state(), _ctx(chili_config))

    assert len(opinions) == len(PERSPECTIVES)
    assert {o.perspective for o in opinions} == set(PERSPECTIVES)
    assert [call[0] for call in gateway.calls] == [TaskType.PANEL] * 3
    assert respx_mock.calls == []


async def test_collect_degrades_when_two_perspectives_fail(
    chili_config: ProcessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两路失败时返回一路意见并记录 warning，不抛异常：由调用方走单链判定。"""
    recorder = _LogRecorder()
    monkeypatch.setattr("app.agent.agents.panel.log", recorder)
    runner, _ = _runner(
        {
            "symptom": _ok("symptom"),
            "environment": RuntimeError("该模型档位不可用"),
            "exclusion": _parse_failure("exclusion"),
        }
    )

    opinions = await runner.collect(chili_config, _state(), _ctx(chili_config))

    assert [o.perspective for o in opinions] == ["symptom"]
    events = [event for event, _ in recorder.events]
    assert events == ["panel quorum not reached, fallback to single chain"]
    assert recorder.events[0][1]["extra"] == {"trace_id": "t-1", "got": 1}


async def test_collect_returns_empty_on_total_failure(chili_config: ProcessConfig) -> None:
    """三路全失败时返回空列表而不是抛异常：面板不可用不该让整条链路崩掉。"""
    runner, _ = _runner({name: RuntimeError("模型档位不可用") for name in PERSPECTIVES})

    assert await runner.collect(chili_config, _state(), _ctx(chili_config)) == []


async def test_collect_times_out_to_single_chain(chili_config: ProcessConfig) -> None:
    """单路超时被当作该路失败，其余两路正常返回：不重试，因为并行本身已是冗余投递。"""

    class _SlowGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
            self.calls += 1
            system = messages[0]["content"]
            if PERSPECTIVE_FOCUS["environment"] in system:
                raise TimeoutError("该视角超时")
            return _ok(self._perspective(system))

        @staticmethod
        def _perspective(system: str) -> str:
            for name, focus in PERSPECTIVE_FOCUS.items():
                if focus in system:
                    return name
            raise AssertionError("提示词里未找到任何视角关注点")

    gateway = _SlowGateway()
    runner = PanelRunner(cast(ModelGateway, gateway), timeout=0.01)

    opinions = await runner.collect(chili_config, _state(), _ctx(chili_config))

    assert {o.perspective for o in opinions} == {"symptom", "exclusion"}
    assert gateway.calls == 3


async def test_already_ran_follows_state_flag(chili_config: ProcessConfig) -> None:
    """`already_ran` 由调用方决定是否重跑：面板只提供状态查询。"""
    runner, _ = _runner({})

    assert runner.already_ran(_state()) is False
    assert runner.already_ran(_state(panel_ran=True)) is True


async def test_opinion_alone_does_not_force_uncertain(chili_config: ProcessConfig) -> None:
    """一路意见就能被裁定：单链兜底时仍按硬判据/排除法给结论，而不是一律不确定。"""
    runner, _ = _runner({"symptom": _ok("symptom", decisive_feature_hit="维管束黄褐色")})

    opinions = await runner.collect(chili_config, _state(), _ctx(chili_config))
    aggregate = aggregate_panel(opinions, chili_config)

    assert aggregate.resolution == "decisive_feature"
    assert aggregate.force_uncertain is False


# ---- 提示词 ----


def test_prompt_focuses_each_perspective(chili_config: ProcessConfig) -> None:
    """三路提示词互不覆盖：若三路问得一样，并行就失去意义。"""
    ctx = _ctx(chili_config)

    bodies = [build_panel_prompt(name, chili_config, _state(), ctx)[0]["content"] for name in PERSPECTIVES]

    assert len(set(bodies)) == len(PERSPECTIVES)
    for name, body in zip(PERSPECTIVES, bodies, strict=True):
        assert PERSPECTIVE_FOCUS[name] in body


def test_prompt_restricts_candidate_domain(chili_config: ProcessConfig) -> None:
    """候选域与决定性特征从配置注入：面板只在给定候选里鉴别。"""
    system = build_panel_prompt("symptom", chili_config, _state(), _ctx(chili_config))[0]["content"]

    assert _BACTERIAL_WILT in system
    assert _WILT in system
    assert "挤压切口渗出乳白色菌脓" in system
    assert "excluded_candidates" in system


def test_prompt_injects_slots_facts_and_evidence(chili_config: ProcessConfig) -> None:
    """user 段注入已确认槽位、现场事实与证据摘要，且证据条数有上限。"""
    state = _state(
        observations={"env_source": "none", "observed_facts": {"rain_3d": "连续降雨"}, "note": "暂无环境数据"},
        evidence=[{"knowledge_id": f"kb_{i}", "title": f"辣椒判据 {i}", "content": "判据正文"} for i in range(10)],
    )

    user = build_panel_prompt("symptom", chili_config, state, _ctx(chili_config))[1]["content"]

    assert "连续降雨" in user
    assert "暂无环境数据" in user
    assert "辣椒判据 0" in user
    assert "辣椒判据 9" not in user


def test_prompt_marks_missing_evidence(chili_config: ProcessConfig) -> None:
    """证据为空时提示词里写明未命中：模型才知道只能靠槽位与现场事实判断。"""
    user = build_panel_prompt("symptom", chili_config, _state(), _ctx(chili_config))[1]["content"]

    assert "未命中" in user


# ---- 下游协作：T2-19 读取 force_uncertain ----


async def test_force_uncertain_flag_is_consumed_by_diagnoser(chili_config: ProcessConfig) -> None:
    """面板与判定的接口是 `state["panel_force_uncertain"]`：写入后判定据此强制不确定。"""
    from app.agent.agents.diagnoser import DiagnoserAgent
    from app.agent.budget import ConsultBudget
    from app.llm import build_gateway

    opinions = [
        _opinion(perspective="symptom", candidate=_BACTERIAL_WILT),
        _opinion(perspective="environment", candidate=_WILT),
    ]
    aggregate = aggregate_panel(opinions, chili_config)
    assert aggregate.force_uncertain is True

    state = _state(evidence=[{"knowledge_id": "kb_1", "title": "判据", "content": "正文"}])
    cast(dict[str, Any], state)["panel_force_uncertain"] = aggregate.force_uncertain
    diagnoser = DiagnoserAgent(build_gateway(), ConsultBudget())

    result = await diagnoser.run(state, _ctx(chili_config))

    assert result.confidence_level == "uncertain"
    assert any("并行会诊" in source for source in result.uncertainty_source)
