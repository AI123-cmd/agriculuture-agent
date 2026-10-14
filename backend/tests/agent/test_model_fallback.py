"""②-B：知识库为空时的模型兜底（零闸门改动）。

用例按七组组织：

1. 触发：开关开启 + 库空 + 未命中原因恰为"确实没有"→ 写 `model_basis`、计数、提示词带硬约束；
2. 不触发：开关关闭、有检索命中、检索故障降级、油菜流程——四种情形都不写、不计数；
3. 跨流程对齐：模型给出的 `process_id` 与当前流程不一致时由代码强制对齐；
4. 静默回落：兜底调用抛错或超时都只看日志，不改 `EvidenceSet`、不抛异常、不计数；
5. 契约护栏：`ModelBasis` 丢弃含剂量数字的条目（含"倍"），并保序去重；
6. 处方消费：药名织入**非药剂处置建议**文本、`pesticide_items` 恒空、免责由代码写死；
7. 零闸门改动：上述处方在 bundle 全量规则下放行；同一动作若混进剂量则被闸门拦下。

网关、知识库与地区匹配器都是替身。开关与超时是运维期配置，通过环境变量加
`get_settings.cache_clear()` 切换——被测代码刻意在调用点读配置而非在构造时缓存。
"""

import asyncio
import json
from typing import Any, cast

import pytest

import app.agent.agents.retriever as retriever_module
from app.agent.agents.base import AgentContext
from app.agent.agents.prescriber import PrescriberAgent
from app.agent.agents.retriever import _FALLBACK_METRIC, _UNMATCHED, RetrieverAgent
from app.agent.contracts import (
    EvidenceItem,
    EvidenceQuery,
    EvidenceSet,
    ModelBasis,
    PesticideItem,
    Prescription,
)
from app.agent.prompts.diagnoser import build_diagnoser_prompt
from app.agent.prompts.prescriber import build_prescriber_prompt
from app.agent.state import ConsultationState, ProcessId, SlotValue, TaskType
from app.core.config import get_settings
from app.gate.contracts import GatePayload, GatePrescription
from app.gate.engine import GateEngine
from app.gate.rules.provenance import UNIT_PATTERN
from app.llm import ModelGateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig
from tests.gate.fixtures.factories import gate_context, prescription

_MESSAGE = "我家水稻叶子发黄，这几天下雨"
_REGION_CODE = "330102"
# 地区回退链：县、市、省、全国。假匹配器与 T2-50 的实现输出同一形态。
_CHAIN = [("county", "330102"), ("city", "330100"), ("province", "330000"), ("national", "000000")]


def _item(**overrides: Any) -> EvidenceItem:
    """一条水稻流程的知识条目。命中它即代表"库里确实有"。"""
    data: dict[str, Any] = {
        "knowledge_id": "kb_rice_001",
        "title": "水稻稻瘟病防治",
        "content": "分蘖期注意排水晒田。",
        "process_id": "rice",
        "region_scope": "county:330102",
        "effective_date": None,
        "kind": "pest",
        "score": 0.87,
    }
    data.update(overrides)
    return EvidenceItem(**data)


def _rewrite_response(text: str) -> ModelResponse:
    """改写调用的成功响应：只带自由文本，不带 `parsed`（改写不套 Schema）。"""
    return ModelResponse(
        content=text,
        parsed=None,
        model="mock-model",
        provider="mock",
        prompt_tokens=5,
        completion_tokens=5,
        latency_ms=1,
    )


def _basis(**overrides: Any) -> ModelBasis:
    """一份合法的兜底依据。默认四条清单都有内容，因此必然写入 state。"""
    data: dict[str, Any] = {
        "process_id": "rice",
        "suggested_pesticide_names": ["吡虫啉"],
        "basis_points": ["叶片黄化多与低洼积水有关"],
        "observation_suggestions": ["观察新叶颜色是否恢复"],
        "help_suggestions": ["若三日内继续扩散请联系当地农技站"],
    }
    data.update(overrides)
    return ModelBasis.model_validate(data)


def _fallback_response(basis: ModelBasis) -> ModelResponse:
    """兜底调用的成功响应：`parsed` 已是 `ModelBasis` 契约对象。"""
    return ModelResponse(
        content=json.dumps(basis.model_dump(mode="json"), ensure_ascii=False),
        parsed=basis,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


class _FakeGateway:
    """回放预设结果的网关替身。最后一次结果被后续所有调用复用。"""

    def __init__(self, results: list[ModelResponse | Exception]) -> None:
        self._results = results
        self.calls: list[tuple[object, list[dict], object]] = []

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.calls.append((task_type, list(messages), schema))
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def last_messages(self) -> list[dict]:
        return self.calls[-1][1]


class _SlowGateway(_FakeGateway):
    """第二次调用故意变慢的网关替身，用于触发 `asyncio.wait_for` 超时。"""

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.calls.append((task_type, list(messages), schema))
        if len(self.calls) >= 2:
            await asyncio.sleep(0.5)
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class _FakeKnowledge:
    """知识库检索接口的替身：按地区编码回放条目，或整体抛一个异常。"""

    def __init__(self, by_region: dict[str, list[EvidenceItem]] | Exception) -> None:
        self._by_region = by_region
        self.calls: list[tuple[str | None, str]] = []

    async def search(self, query: EvidenceQuery, region_chain: list[str]) -> list[EvidenceItem]:
        self.calls.append((query.region_code, query.process_id.value))
        if isinstance(self._by_region, Exception):
            raise self._by_region
        return list(self._by_region.get(query.region_code or "", []))


class _FakeRegionMatcher:
    """地区回退链构造接口的替身。"""

    def __init__(self, chain: list[tuple[str, str]] | None = None) -> None:
        self._chain = list(chain if chain is not None else _CHAIN)

    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        return list(self._chain)


class _LogRecorder:
    """记录结构化日志事件的替身。`info`/`warning` 都收，便于断言"用了"或"跳过了"。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(autouse=True)
def _reset_settings():
    """每个用例结束后清配置缓存：开关与超时是环境变量驱动的，避免串到下一个用例。"""
    yield
    get_settings.cache_clear()


@pytest.fixture()
def logs(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """把 retriever 模块里的 `log` 换成记录器：structlog 默认输出到 stdout，不便断言。"""
    recorder = _LogRecorder()
    monkeypatch.setattr(retriever_module, "log", recorder)
    return recorder


@pytest.fixture()
def fallback_on(monkeypatch: pytest.MonkeyPatch):
    """开启兜底开关。改环境变量后必须清缓存才会重新读取。"""
    monkeypatch.setenv("ALLOW_MODEL_FALLBACK", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def rice_config() -> ProcessConfig:
    return get_process_config(ProcessId.RICE)


@pytest.fixture(scope="module")
def rapeseed_config() -> ProcessConfig:
    return get_process_config(ProcessId.RAPESEED)


def _ctx(config: ProcessConfig, task_type: TaskType = TaskType.RETRIEVE) -> AgentContext:
    return AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=task_type,
        trace_id="t-1",
        region_code=_REGION_CODE,
    )


def _retriever(
    results: list[ModelResponse | Exception],
    *,
    knowledge: dict[str, list[EvidenceItem]] | Exception | None = None,
) -> tuple[RetrieverAgent, _FakeGateway]:
    gateway = _FakeGateway(results)
    store = _FakeKnowledge(knowledge if knowledge is not None else {})
    agent = RetrieverAgent(cast(ModelGateway, gateway), store, _FakeRegionMatcher())
    return agent, gateway


def _prescriber(
    results: list[ModelResponse | Exception],
    *,
    by_region: dict[str, list[EvidenceItem]] | Exception | None = None,
) -> tuple[PrescriberAgent, _FakeGateway]:
    gateway = _FakeGateway(results)
    store = _FakeKnowledge(by_region if by_region is not None else {})
    agent = PrescriberAgent(cast(ModelGateway, gateway), store, _FakeRegionMatcher())
    return agent, gateway


def _slot(key: str, status: str, value: str | None = None) -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source="user_answer")  # type: ignore[arg-type]


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _MESSAGE}],
        "slots": {"symptom": _slot("symptom", "filled", "叶片发黄")},
    }
    state.update(overrides)
    return state


def _diagnosis() -> dict[str, Any]:
    return {
        "conclusion": "水稻叶片黄化",
        "confidence_level": "preliminary",
        "basis": ["叶片自下而上黄化"],
        "uncertainty_source": [],
        "excluded": ["稻瘟病"],
        "terminology_profile": "rice",
        "needs_more_info": [],
        "out_of_scope": False,
        "boundary_reason": None,
    }


def _prescription_response(**overrides: Any) -> ModelResponse:
    """处方模型侧的最小合法输出：一条无药剂的处置动作。"""
    data: dict[str, Any] = {
        "diagnosis_summary": "水稻叶片黄化，本地暂无可用条目",
        "actions": [
            {
                "order": 1,
                "action": "先开沟排水晾田",
                "timing": "尽快",
                "method": "按地块低洼处开沟排水",
                "cautions": ["避免高温时段作业"],
                "stop_conditions": ["叶片继续大面积黄化时联系农技站"],
            }
        ],
        "safety_interval_days": None,
        "max_uses_per_season": None,
        "pesticide_items": [],
        "soil_level_actions": [],
        "followup_plan": {"tasks": [], "note": None},
        "facts_provenance": {},
        "requires_tech_station": False,
        "detail_terms": {},
    }
    data.update(overrides)
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=Prescription.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


def _fallback_metric(rice: bool = True) -> int:
    return metrics.counter_value(_FALLBACK_METRIC, tags={"process_id": "rice" if rice else "tea"})


# ---- 第一组：触发 ----


async def test_library_empty_writes_model_basis_and_counts(
    rice_config: ProcessConfig, logs: _LogRecorder, fallback_on: None
) -> None:
    """库空 + 开关开启：兜底产出写进 `model_basis`、上报计数、且走检索档位的 Schema 校验。"""
    agent, gateway = _retriever([_rewrite_response("叶片"), _fallback_response(_basis())])
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert result.hit is False
    assert result.items == []
    assert result.unmatched_reason == _UNMATCHED
    assert state["model_basis"]["suggested_pesticide_names"] == ["吡虫啉"]
    assert state["model_basis"]["process_id"] == "rice"
    # 第二次调用是兜底，且带 `ModelBasis` 作为结构化输出 Schema。
    assert gateway.calls[1][2] is ModelBasis
    assert _fallback_metric() == 1
    assert [event for event, _ in logs.events] == ["model fallback used"]


async def test_fallback_prompt_forbids_dosage_and_marks_no_local_entry(
    rice_config: ProcessConfig, fallback_on: None
) -> None:
    """兜底提示词把"只给药名、禁止任何数字"写进最靠近输出的位置，并说明库里没有条目。"""
    agent, gateway = _retriever([_rewrite_response("叶片"), _fallback_response(_basis())])

    await agent.run(_state(), _ctx(rice_config))

    system = gateway.last_messages[0]["content"]
    user = gateway.last_messages[-1]["content"]
    assert "当前没有可用的当地知识库条目" in system
    assert "禁止出现任何用量" in system
    assert "不得编造任何知识库条目编号" in system
    assert json.dumps(ModelBasis.model_json_schema(), ensure_ascii=False) in system
    for term in rice_config.terminology.display_metrics:
        assert term in system
    # 空库时 user 段明确写"无"，告诉模型这是"请你补充"而不是"检索故障"。
    assert "已有检索条目（可能为空）：无" in user


async def test_empty_basis_is_not_written(
    rice_config: ProcessConfig, logs: _LogRecorder, fallback_on: None
) -> None:
    """四条清单全空视为"没有可用产出"，不写 state、不计数：空对象只会给下游多一个分支。"""
    empty = _basis(
        suggested_pesticide_names=[],
        basis_points=[],
        observation_suggestions=[],
        help_suggestions=[],
    )
    agent, _ = _retriever([_rewrite_response("叶片"), _fallback_response(empty)])
    state = _state()

    await agent.run(state, _ctx(rice_config))

    assert "model_basis" not in state
    assert _fallback_metric() == 0
    assert [event for event, _ in logs.events] == []


# ---- 第二组：不触发 ----


async def test_switch_off_keeps_current_behaviour(rice_config: ProcessConfig) -> None:
    """默认关闭：与现状完全一致——不调模型、不写 state、不计数。"""
    agent, gateway = _retriever([_rewrite_response("叶片"), _fallback_response(_basis())])
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert result.unmatched_reason == _UNMATCHED
    assert "model_basis" not in state
    # 只有改写一次调用，兜底路径根本没进。
    assert len(gateway.calls) == 1
    assert _fallback_metric() == 0


async def test_hit_skips_fallback(rice_config: ProcessConfig, fallback_on: None) -> None:
    """有检索命中就无需补充：兜底只在"库里确实没有"时报效。"""
    agent, gateway = _retriever(
        [_rewrite_response("叶片"), _fallback_response(_basis())],
        knowledge={"330102": [_item()]},
    )
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert result.hit is True
    assert len(result.items) == 1
    assert "model_basis" not in state
    assert len(gateway.calls) == 1
    assert _fallback_metric() == 0


async def test_retrieval_failure_does_not_fallback(rice_config: ProcessConfig, fallback_on: None) -> None:
    """检索故障不兜底：带故障原因的未命中说明链路本身有问题，补内容会掩盖故障。"""
    agent, gateway = _retriever(
        [_rewrite_response("叶片"), _fallback_response(_basis())],
        knowledge=RuntimeError("知识库连接失败"),
    )
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert "证据核查服务不可用" in (result.unmatched_reason or "")
    assert "model_basis" not in state
    assert len(gateway.calls) == 1
    assert _fallback_metric() == 0


async def test_rapeseed_never_falls_back(rapeseed_config: ProcessConfig, fallback_on: None) -> None:
    """油菜流程不兜底：RAPE-01 是产品硬要求，即便只给药名也是推荐类内容。"""
    agent, gateway = _retriever([_rewrite_response("叶片"), _fallback_response(_basis(process_id="rapeseed"))])
    state = _state()

    result = await agent.run(state, _ctx(rapeseed_config))

    assert result.hit is False
    assert "model_basis" not in state
    assert len(gateway.calls) == 1
    assert metrics.counter_value(_FALLBACK_METRIC, tags={"process_id": "rapeseed"}) == 0


# ---- 第三组：跨流程对齐 ----


async def test_model_process_id_is_forced_to_current_process(
    rice_config: ProcessConfig, fallback_on: None
) -> None:
    """模型给出的 `process_id` 不可信：与当前流程不一致时由代码强制改写。"""
    agent, _ = _retriever(
        [_rewrite_response("叶片"), _fallback_response(_basis(process_id="tea"))]
    )
    state = _state()

    await agent.run(state, _ctx(rice_config))

    assert state["model_basis"]["process_id"] == "rice"
    assert _fallback_metric() == 1


# ---- 第四组：静默回落 ----


async def test_gateway_error_is_silent(rice_config: ProcessConfig, logs: _LogRecorder, fallback_on: None) -> None:
    """兜底调用抛错：不抛异常、不改证据集、不计数，只留一条告警。"""
    agent, _ = _retriever([_rewrite_response("叶片"), ValueError("网关未装配")])
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert result.hit is False
    assert result.unmatched_reason == _UNMATCHED
    assert "model_basis" not in state
    assert _fallback_metric() == 0
    assert [event for event, _ in logs.events] == ["model fallback skipped"]
    assert "网关未装配" in logs.events[0][1]["reason"]


async def test_parse_error_response_is_silent(rice_config: ProcessConfig, fallback_on: None) -> None:
    """响应带错误或解析为空：视为没有可用的兜底产出，静默回落。"""
    failure = ModelResponse(
        content="（模型输出不是合法 JSON）",
        parsed=None,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1,
        error="parse_error",
    )
    agent, _ = _retriever([_rewrite_response("叶片"), failure])
    state = _state()

    await agent.run(state, _ctx(rice_config))

    assert "model_basis" not in state
    assert _fallback_metric() == 0


async def test_timeout_falls_back_to_current_behaviour(
    rice_config: ProcessConfig, logs: _LogRecorder, fallback_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时同样静默回落：兜底是可选增强，不让农户为它多等。"""
    monkeypatch.setenv("MODEL_FALLBACK_TIMEOUT_SECONDS", "0.01")
    get_settings.cache_clear()
    gateway = _SlowGateway([_rewrite_response("叶片"), _fallback_response(_basis())])
    agent = RetrieverAgent(cast(ModelGateway, gateway), _FakeKnowledge({}), _FakeRegionMatcher())
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert result.hit is False
    assert "model_basis" not in state
    assert _fallback_metric() == 0
    assert [event for event, _ in logs.events] == ["model fallback skipped"]


# ---- 第五组：契约护栏 ----


def test_model_basis_drops_any_entry_with_dosage() -> None:
    """含剂量数字的条目整条丢弃：剂量必须来自已核准知识库，模型无权写入。"""
    basis = ModelBasis.model_validate(
        {
            "process_id": "rice",
            "suggested_pesticide_names": ["吡虫啉 20克/亩", "吡虫啉", "稀释1000倍使用", "噻嗪酮"],
            "basis_points": ["每亩用 15 毫升", "叶片黄化多与低洼积水有关"],
            "observation_suggestions": [],
            "help_suggestions": [],
        }
    )

    assert basis.suggested_pesticide_names == ["吡虫啉", "噻嗪酮"]
    assert basis.basis_points == ["叶片黄化多与低洼积水有关"]


def test_model_basis_dedupes_and_truncates() -> None:
    """保序去重：模型常在相邻条目重复同一药名，重复会被展示成"用了两次"。"""
    basis = _basis(suggested_pesticide_names=["吡虫啉", "吡虫啉", "噻嗪酮", " "])

    assert basis.suggested_pesticide_names == ["吡虫啉", "噻嗪酮"]


# ---- 第六组：处方消费 ----


async def test_prescription_weaves_names_into_non_pesticide_action(rice_config: ProcessConfig) -> None:
    """空候选 + 兜底依据：药名织入非药剂处置建议，`pesticide_items` 恒空。"""
    agent, _ = _prescriber([_prescription_response()], by_region={})
    state = _state(diagnosis=_diagnosis(), model_basis=_basis().model_dump(mode="json"))

    result = await agent.run(state, _ctx(rice_config, TaskType.PRESCRIBE))

    assert result.pesticide_items == []
    assert result.safety_interval_days is None
    assert result.max_uses_per_season is None
    assert result.facts_provenance == {}
    assert result.requires_tech_station is True
    assert result.numeric_paths() == []
    fallback_actions = [a for a in result.actions if "吡虫啉" in a.action]
    assert len(fallback_actions) == 1
    action = fallback_actions[0]
    assert action.order == 2
    assert action.stop_conditions
    joined = " ".join([action.action, action.timing, action.method, *action.cautions, *action.stop_conditions])
    assert UNIT_PATTERN.search(joined) is None


async def test_prescription_without_basis_keeps_tech_station_shape(rice_config: ProcessConfig) -> None:
    """无兜底依据时不追加动作，保持"空库转农技站"的既有形态。"""
    agent, _ = _prescriber([_prescription_response()], by_region={})
    state = _state(diagnosis=_diagnosis())

    result = await agent.run(state, _ctx(rice_config, TaskType.PRESCRIBE))

    assert result.pesticide_items == []
    assert result.requires_tech_station is True
    assert [a.action for a in result.actions] == ["先开沟排水晾田"]


async def test_prescription_candidates_suppress_fallback_names(rice_config: ProcessConfig) -> None:
    """有知识库候选时不注入兜底药名：两者并列会让模型在"照抄"与"参考"之间摇摆。"""
    candidate = PesticideItem(knowledge_id="kb_rice_001", name="示例药剂", dosage_min=10.0, dosage_max=20.0)
    user = build_prescriber_prompt(
        rice_config,
        _state(diagnosis=_diagnosis(), model_basis=_basis().model_dump(mode="json")),
        [candidate],
    )[1]["content"]

    assert "吡虫啉" not in user


async def test_prescriber_prompt_injects_names_and_hard_rule(rice_config: ProcessConfig) -> None:
    """有兜底依据时提示词追加药名段与"不得写数字"硬规则。"""
    messages = build_prescriber_prompt(
        rice_config,
        _state(diagnosis=_diagnosis(), model_basis=_basis().model_dump(mode="json")),
        [],
    )

    assert "吡虫啉" in messages[1]["content"]
    assert "不得为它们写任何用量" in messages[0]["content"]


def test_prescriber_prompt_ignores_cross_process_basis(rice_config: ProcessConfig) -> None:
    """跨流程的兜底依据不得被套用：术语与判据不能跨流程混用。"""
    user = build_prescriber_prompt(
        rice_config,
        _state(diagnosis=_diagnosis(), model_basis=_basis(process_id="tea").model_dump(mode="json")),
        [],
    )[1]["content"]

    assert "吡虫啉" not in user


# ---- 第七组：零闸门改动 ----


def test_diagnoser_prompt_marks_fallback_basis_as_unverified(rice_config: ProcessConfig) -> None:
    """判定侧把兜底判据单独标注：它不是已核准条目，不能当作知识库事实引用。"""
    user = build_diagnoser_prompt(
        rice_config,
        _state(model_basis=_basis().model_dump(mode="json")),
        [],
    )[1]["content"]

    assert "未经本地资料核实" in user
    assert "叶片黄化多与低洼积水有关" in user


def test_diagnoser_prompt_omits_fallback_without_basis(rice_config: ProcessConfig) -> None:
    """没有兜底依据时不多出注入段。"""
    user = build_diagnoser_prompt(rice_config, _state(), [])[1]["content"]

    assert "未经本地资料核实" not in user


async def test_fallback_prescription_passes_gate_without_any_change(rice_config: ProcessConfig) -> None:
    """②-B 的核心收益：空库 + 兜底药名的处方在 bundle 全量规则下 `allow`，闸门一行未改。"""
    agent, _ = _prescriber([_prescription_response()], by_region={})
    state = _state(diagnosis=_diagnosis(), model_basis=_basis().model_dump(mode="json"))

    result = await agent.run(state, _ctx(rice_config, TaskType.PRESCRIBE))
    mirror = GatePrescription.from_contract(result)
    verdict = GateEngine().evaluate(gate_context(prescription=mirror))

    assert mirror.pesticide_items == []
    assert verdict.decision == "allow"
    assert verdict.passed is True
    assert verdict.violations == []


def test_gate_still_blocks_dosage_leaked_into_display_text() -> None:
    """反向对照：展示文本一旦混进剂量即 block——护栏不依赖"模型守约"这个约定。

    先执行的是 `R-NO-DATA`（正向核对 + 文本扫描，同为 block 级），它命中后引擎短路，
    因此这里断言的是它；`R-PROVENANCE` 是同一份 `UNIT_PATTERN` 上的另一道扫描。
    """
    leaky = prescription(items=[], safety_interval_days=None)
    ctx = gate_context(
        prescription=leaky,
        payload=GatePayload(rendered_text="可考虑使用这些药剂：吡虫啉 20克"),
    )

    verdict = GateEngine().evaluate(ctx)

    assert verdict.decision == "block"
    assert any(v.rule_id == "R-NO-DATA" for v in verdict.violations)
    assert UNIT_PATTERN.search("可考虑使用这些药剂：吡虫啉 20克") is not None


async def test_fallback_does_not_touch_evidence_set_or_knowledge(
    rice_config: ProcessConfig, fallback_on: None
) -> None:
    """兜底永不写入知识库、不改 `EvidenceSet`：它只是本次问诊的临时输入。"""
    agent, _ = _retriever([_rewrite_response("叶片"), _fallback_response(_basis())])
    state = _state()

    result = await agent.run(state, _ctx(rice_config))

    assert isinstance(result, EvidenceSet)
    assert result.items == []
    assert result.hit is False
