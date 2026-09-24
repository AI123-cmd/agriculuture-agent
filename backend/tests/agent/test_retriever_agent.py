"""T2-18：证据核查智能体的硬过滤、地区回退与空集标注。

用例分七组：

1. 检索请求：`process_id` 为硬过滤（恒等于流程配置）、油菜排除 `pesticide`、`top_k`
   随是否启用并行会诊变化、关键词与槽位摘要进请求；
2. 关键词改写：失败时原样返回且不降级，成功时是**增量**追加，空关键词不调模型；
3. 地区回退：县级无结果时回退到市级并记下两级尝试链，全链未命中时返回带原因的空集；
4. 硬过滤：混入其他流程的条目被丢弃，通用流程的条目保留；
5. 组装：`post_validate` 按 `knowledge_id` 去重后按分值截断到 `top_k`，空集补齐未命中原因；
6. 降级：检索失败时返回空集、`region_level_actual="none"` 且 `unmatched_reason` 非空；
7. mock 模式与零网络请求。

网关、知识库与地区匹配器都是替身：本智能体在这三者面前是**被调用方**，真正的检索走
T2-49 还是假实现与它的行为无关。另有一条 `respx` 零请求断言。
"""

import json
from datetime import date
from typing import Any, cast

import pytest
import respx

import app.agent.agents.retriever as retriever_module
from app.agent.agents.base import AgentContext
from app.agent.agents.retriever import (
    _TOP_K_DEFAULT,
    _TOP_K_PANEL,
    _UNMATCHED,
    RetrieverAgent,
    build_rewrite_prompt,
)
from app.agent.contracts import EvidenceItem, EvidenceQuery, EvidenceSet
from app.agent.state import ConsultationState, ProcessId, SlotValue, TaskType
from app.llm import ModelGateway, build_gateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_MESSAGE = "我家茶树叶子发黄，这几天一直没下雨"
_REGION_CODE = "330102"
# 地区回退链：县、市、省、全国。假匹配器与 T2-50 的实现输出同一形态。
_CHAIN = [("county", "330102"), ("city", "330100"), ("province", "330000"), ("national", "000000")]


def _item(**overrides: Any) -> EvidenceItem:
    """一条茶流程的知识条目。`region_scope` 用 T2-50 的 `"{level}:{code}"` 标注形式。"""
    data: dict[str, Any] = {
        "knowledge_id": "kb_tea_001",
        "title": "茶小绿叶蝉防治",
        "content": "若虫期用药效果最好。",
        "process_id": "tea",
        "region_scope": "county:330102",
        "effective_date": date(2025, 3, 1),
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


class _FakeKnowledge:
    """知识库检索接口的替身：按地区编码回放条目，或整体抛一个异常。

    记录每次调用的地区编码与完整回退链，用于断言"检索实现收到的是单级地区、整条链
    只作为加权输入"。
    """

    def __init__(self, by_region: dict[str, list[EvidenceItem]] | Exception) -> None:
        self._by_region = by_region
        self.calls: list[tuple[str | None, str]] = []
        self.region_chains: list[list[str]] = []

    async def search(self, query: EvidenceQuery, region_chain: list[str]) -> list[EvidenceItem]:
        self.calls.append((query.region_code, query.process_id.value))
        self.region_chains.append(list(region_chain))
        if isinstance(self._by_region, Exception):
            raise self._by_region
        return list(self._by_region.get(query.region_code or "", []))


class _FakeRegionMatcher:
    """地区回退链构造接口的替身。"""

    def __init__(self, chain: list[tuple[str, str]] | None = None) -> None:
        self._chain = list(chain if chain is not None else _CHAIN)
        self.codes: list[str | None] = []

    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        self.codes.append(region_code)
        return list(self._chain)


class _LogRecorder:
    """记录结构化日志事件的替身，用于断言"丢弃跨流程条目时确实告警"。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def logs(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """把 retriever 模块里的 `log` 换成记录器：structlog 默认输出到 stdout，不便断言。"""
    recorder = _LogRecorder()
    monkeypatch.setattr(retriever_module, "log", recorder)
    return recorder


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(ProcessId.TEA)


@pytest.fixture(scope="module")
def rapeseed_config() -> ProcessConfig:
    return get_process_config(ProcessId.RAPESEED)


def _ctx(config: ProcessConfig) -> AgentContext:
    return AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.RETRIEVE,
        trace_id="t-1",
        region_code=_REGION_CODE,
    )


@pytest.fixture()
def ctx(tea_config: ProcessConfig) -> AgentContext:
    return _ctx(tea_config)


def _agent(
    results: list[ModelResponse | Exception],
    *,
    knowledge: dict[str, list[EvidenceItem]] | Exception | None = None,
    chain: list[tuple[str, str]] | None = None,
) -> tuple[RetrieverAgent, _FakeGateway, _FakeKnowledge, _FakeRegionMatcher]:
    gateway = _FakeGateway(results)
    store = _FakeKnowledge(knowledge if knowledge is not None else {})
    region = _FakeRegionMatcher(chain)
    agent = RetrieverAgent(cast(ModelGateway, gateway), store, region)
    return agent, gateway, store, region


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


def _slot(key: str, status: str, value: str | None = None, source: str = "user_answer") -> SlotValue:
    return SlotValue(key=key, value=value, status=status, source=source)  # type: ignore[arg-type]


# ---- 第一组：检索请求 ----


def test_query_process_id_follows_ctx(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """`process_id` 是硬过滤，恒定取流程配置的值，不受状态影响。"""
    agent, _, _, _ = _agent([])

    query = agent.build_query(_state(), ctx)

    assert query.process_id is tea_config.process_id
    assert query.process_id is ProcessId.TEA
    assert query.region_code == _REGION_CODE


def test_kind_filter_excludes_pesticide_for_rapeseed(rapeseed_config: ProcessConfig) -> None:
    """油菜在候选集层面就不召回药剂条目：红线是第二道防线。"""
    agent, _, _, _ = _agent([])

    kinds = agent.kind_filter_for(rapeseed_config)

    assert "pesticide" not in kinds
    assert kinds == ["pest", "disease", "cultivation", "threshold"]
    # 其余流程不限定条目类型。
    assert agent.kind_filter_for(get_process_config(ProcessId.TEA)) == []


def test_top_k_follows_panel_enabled(tea_config: ProcessConfig, rapeseed_config: ProcessConfig) -> None:
    """启用并行会诊的流程要更大的证据空间，其余流程取默认值。"""
    agent, _, _, _ = _agent([])

    assert tea_config.panel_enabled is True
    assert agent._top_k_for(tea_config) == _TOP_K_PANEL  # noqa: SLF001
    assert rapeseed_config.panel_enabled is False
    assert agent._top_k_for(rapeseed_config) == _TOP_K_DEFAULT  # noqa: SLF001


def test_query_carries_keywords_and_slot_snapshot(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """关键词含已填槽位值与流程指标口径，顺序保序（高优先来源在前）。"""
    agent, _, _, _ = _agent([])
    state = _state(
        slots={"tea_symptom_part": _slot("tea_symptom_part", "filled", "叶片")},
        observations={"env_source": "none", "observed_facts": {"rain_last_3d": "近三天没有下雨"}},
    )

    query = agent.build_query(state, ctx)

    assert query.keywords[0] == "叶片"
    for metric_name in tea_config.terminology.display_metrics:
        assert metric_name in query.keywords
    for pair in tea_config.confusable_items:
        for candidate in pair.candidates:
            assert candidate in query.keywords
    assert "近三天没有下雨" in query.keywords
    assert query.slot_snapshot == {"tea_symptom_part": "叶片"}
    assert query.top_k == _TOP_K_PANEL


def test_filled_slot_values_are_deduped(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """槽位值与现场事实重复时只留一份：重复词会稀释检索权重。"""
    agent, _, _, _ = _agent([])
    state = _state(
        slots={
            "tea_symptom_part": _slot("tea_symptom_part", "filled", "叶片卷曲"),
            "tea_symptom_pattern": _slot("tea_symptom_pattern", "filled", "叶片卷曲"),
        }
    )

    keywords = agent.build_query(state, ctx).keywords

    assert keywords.count("叶片卷曲") == 1


# ---- 第二组：关键词改写 ----


async def test_rewrite_failure_returns_raw_keywords(tea_config: ProcessConfig, logs: _LogRecorder) -> None:
    """改写是可选增强：网关抛错时原样返回，不抛异常也不降级。"""
    agent, _, _, _ = _agent([ValueError("网关未装配")])

    result = await agent.rewrite_keywords(["叶子起油", "卷曲"], tea_config)

    assert result == ["叶子起油", "卷曲"]
    assert [event for event, _ in logs.events] == ["keyword rewrite failed, use raw"]
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "retriever", "mode": "degrade"}) == 0


async def test_rewrite_error_response_is_treated_as_no_rewrite(tea_config: ProcessConfig) -> None:
    """响应带错误时视为没有可用的改写结果，原始关键词仍完整保留。"""
    agent, _, _, _ = _agent(
        [
            ModelResponse(
                content=None,
                parsed=None,
                model="mock-model",
                provider="mock",
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=0,
                error="parse_error",
            )
        ]
    )

    assert await agent.rewrite_keywords(["叶子起油"], tea_config) == ["叶子起油"]


async def test_rewrite_output_is_incremental(tea_config: ProcessConfig) -> None:
    """改写结果追加在原词之后而不是替换：改写偏离时原始表述仍在请求里。"""
    agent, _, _, _ = _agent([_rewrite_response("茶饼病、萎蔫")])

    result = await agent.rewrite_keywords(["叶子起油", "蔫了"], tea_config)

    assert isinstance(result, list)
    for word in ("叶子起油", "蔫了"):
        assert word in result
    assert result[:2] == ["叶子起油", "蔫了"]
    assert set(result[2:]) == {"茶饼病", "萎蔫"}


async def test_rewrite_drops_non_word_fragments(tea_config: ProcessConfig) -> None:
    """改写文本里的括号片段与标点片段不是检索用词，不得进入请求。"""
    agent, _, _, _ = _agent([_rewrite_response("茶饼病、萎蔫、（）、、; 卷曲")])

    result = await agent.rewrite_keywords(["叶子起油"], tea_config)

    assert result == ["叶子起油", "茶饼病", "萎蔫", "卷曲"]


async def test_rewrite_empty_keywords_skips_gateway(tea_config: ProcessConfig) -> None:
    """没有关键词时不必调模型：省一次调用也避免模型凭空补词。"""
    agent, gateway, _, _ = _agent([_rewrite_response("茶饼病")])

    assert await agent.rewrite_keywords([], tea_config) == []
    assert gateway.calls == []


async def test_rewrite_prompt_carries_internal_terms(tea_config: ProcessConfig) -> None:
    """提示词注入流程的 `internal_terms` 与待改写的原词，改写口径随流程配置变化。"""
    agent, gateway, _, _ = _agent([_rewrite_response("茶饼病")])

    await agent.rewrite_keywords(["叶子起油"], tea_config)

    system, user = gateway.last_messages[0]["content"], gateway.last_messages[-1]["content"]
    assert json.dumps(tea_config.terminology.internal_terms, ensure_ascii=False) in system
    assert user == "叶子起油"


def test_build_rewrite_prompt_is_public(tea_config: ProcessConfig) -> None:
    """提示词构造是公开函数：T2-60 统一迁移提示词时直接复用，无需改调用方。"""
    messages = build_rewrite_prompt(["叶子起油"], tea_config)

    assert [m["role"] for m in messages] == ["system", "user"]


# ---- 第三组：检索与地区回退 ----


async def test_county_miss_falls_back_to_city(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """县级无结果时回退到市级，尝试链保留两级而不是只留终点。"""
    agent, gateway, store, region = _agent(
        [_rewrite_response("叶片")],
        knowledge={"330100": [_item(region_scope="city:330100")]},
    )

    result = await agent.run(_state(), ctx)

    assert result.region_level_actual == "city"
    assert result.fallback_chain == ["county:330102", "city:330100"]
    assert result.hit is True
    assert [item.knowledge_id for item in result.items] == ["kb_tea_001"]
    # 逐级检索、命中即止：省级与全国级没有被查。
    assert [code for code, _ in store.calls] == ["330102", "330100"]
    assert region.codes == [_REGION_CODE]
    assert len(gateway.calls) == 1


async def test_search_receives_single_region_and_full_chain(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """检索实现收到单级地区编码与整条回退链：回退判定在智能体侧，链只用于加权。"""
    agent, _, store, _ = _agent([_rewrite_response("叶片")], knowledge={"330102": [_item()]})

    await agent.run(_state(), ctx)

    assert store.calls == [("330102", "tea")]
    assert store.region_chains == [["330102", "330100", "330000", "000000"]]


async def test_no_hit_returns_empty_set_with_reason(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """四级都没命中是合法结果，但必须带原因：下游据此降低确定性表述。"""
    agent, _, store, _ = _agent([_rewrite_response("叶片")])

    result = await agent.run(_state(), ctx)

    assert result.hit is False
    assert result.items == []
    assert result.region_level_actual == "none"
    assert result.unmatched_reason == _UNMATCHED
    assert result.fallback_chain == [
        "county:330102",
        "city:330100",
        "province:330000",
        "national:000000",
    ]
    assert [code for code, _ in store.calls] == ["330102", "330100", "330000", "000000"]


async def test_query_uses_rewritten_keywords(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """改写结果进入检索请求，且原始关键词仍在其中。"""
    agent, _, store, _ = _agent([_rewrite_response("茶饼病")], knowledge={"330102": [_item()]})

    result = await agent.run(_state(), ctx)

    assert "茶饼病" in result.query.keywords
    assert "叶发病率" in result.query.keywords
    assert store.calls == [("330102", "tea")]


# ---- 第四组：跨流程硬过滤 ----


async def test_cross_process_items_are_dropped(
    tea_config: ProcessConfig, ctx: AgentContext, logs: _LogRecorder
) -> None:
    """检索侧漏出的辣椒条目在组装前被丢弃，并留下告警。"""
    agent, _, _, _ = _agent(
        [_rewrite_response("叶片")],
        knowledge={
            "330102": [
                _item(knowledge_id="kb_tea_001", score=0.9),
                _item(knowledge_id="kb_chili_009", process_id="chili", kind="pesticide", score=0.95),
            ]
        },
    )

    result = await agent.run(_state(), ctx)

    assert [item.knowledge_id for item in result.items] == ["kb_tea_001"]
    assert result.hit is True
    dropped = [fields for event, fields in logs.events if event == "cross-process evidence dropped"]
    assert dropped == [{"knowledge_id": "kb_chili_009", "process_id": "chili"}]


def test_general_items_are_kept(tea_config: ProcessConfig) -> None:
    """通用流程的条目是兜底知识，允许混入任意流程。

    这里只断言过滤函数本身：T2-10 契约的 `_all_items_same_process` 只允许本流程，
    故通用条目不得进入 `EvidenceSet` 的构造路径。
    """
    agent, _, _, _ = _agent([])

    kept = agent.enforce_process_scope(
        [
            _item(knowledge_id="kb_tea_001"),
            _item(knowledge_id="kb_general_002", process_id="general", region_scope=None),
            _item(knowledge_id="kb_rice_003", process_id="rice"),
        ],
        ProcessId.TEA,
    )

    assert [item.knowledge_id for item in kept] == ["kb_tea_001", "kb_general_002"]


# ---- 第五组：组装、去重与截断 ----


async def test_post_validate_dedupes_and_truncates(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """同一份证据集在 `post_validate` 前后按 `knowledge_id` 去重，且条数不超过 `top_k`。"""
    agent, _, _, _ = _agent([])
    state = _state()
    items = [_item(knowledge_id=f"kb_{i:02d}", score=0.5 + i * 0.01) for i in range(1, 13)]
    # 混合检索的另一条通路对 kb_11 给出低分副本：去重保留分值更高者。
    items.append(_item(knowledge_id="kb_11", score=0.01))
    result = EvidenceSet(
        query=agent.build_query(state, ctx),
        items=items,
        hit=True,
        region_level_actual="county",
        fallback_chain=["county:330102"],
    )

    validated = await agent.post_validate(result, state, ctx)

    ids = [item.knowledge_id for item in validated.items]
    assert len(ids) == _TOP_K_PANEL
    assert len(set(ids)) == len(ids)
    assert ids == [f"kb_{i:02d}" for i in range(12, 2, -1)]
    # 重复条目保留分值更高者。
    kept = {item.knowledge_id: item.score for item in validated.items}
    assert kept["kb_11"] == pytest.approx(0.61)
    assert validated.hit is True


async def test_post_validate_fills_reason_for_empty_set(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """空集缺原因时补上：契约的 `_miss_requires_reason` 是兜底，本层要真的填。"""
    agent, _, _, _ = _agent([])
    state = _state()
    result = EvidenceSet(
        query=agent.build_query(state, ctx),
        items=[],
        hit=True,
        region_level_actual="county",
        fallback_chain=["county:330102"],
    )

    validated = await agent.post_validate(result, state, ctx)

    assert validated.hit is False
    assert validated.items == []
    assert validated.unmatched_reason == _UNMATCHED


async def test_post_validate_keeps_existing_reason(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """已有原因时不覆盖：降级给出的"服务不可用"与"库里确实没有"要能区分。"""
    agent, _, _, _ = _agent([])
    state = _state()
    result = EvidenceSet(
        query=agent.build_query(state, ctx),
        items=[],
        hit=False,
        region_level_actual="none",
        unmatched_reason="证据核查服务不可用（离线维护）",
    )

    validated = await agent.post_validate(result, state, ctx)

    assert validated.unmatched_reason == "证据核查服务不可用（离线维护）"


async def test_post_validate_sorts_by_score(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """同分之外一律按分值降序，排在最前的是最相关的条目。"""
    agent, _, _, _ = _agent([])
    state = _state()
    result = EvidenceSet(
        query=agent.build_query(state, ctx),
        items=[
            _item(knowledge_id="kb_low", score=0.2),
            _item(knowledge_id="kb_high", score=0.95),
            _item(knowledge_id="kb_mid", score=0.6),
        ],
        hit=True,
        region_level_actual="county",
    )

    validated = await agent.post_validate(result, state, ctx)

    assert [item.knowledge_id for item in validated.items] == ["kb_high", "kb_mid", "kb_low"]


# ---- 第六组：降级 ----


async def test_search_failure_returns_empty_set_with_reason(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """检索失败时返回空集并标注原因，不抛异常也不重试。"""
    agent, gateway, _, _ = _agent([_rewrite_response("叶片")], knowledge=RuntimeError("知识库连接失败"))

    result = await agent.run(_state(), ctx)

    assert result.hit is False
    assert result.items == []
    assert result.region_level_actual == "none"
    assert result.unmatched_reason is not None
    assert "证据核查服务不可用" in result.unmatched_reason
    assert "RuntimeError" in result.unmatched_reason
    assert result.fallback_chain == []
    assert result.query.process_id is ProcessId.TEA
    # 改写成功一次即进入检索，检索失败后不再重试。
    assert len(gateway.calls) == 1
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "retriever", "mode": "degrade"}) == 1


async def test_degrade_marks_none_level(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """降级空集的级别标注是 `none`，界面据此说明"暂时查不到适合您当地的条目"。"""
    agent, _, _, _ = _agent([])

    result = await agent.degrade(_state(), ctx, "KnowledgeUnavailableError: 向量化服务超时")

    assert result.hit is False
    assert result.region_level_actual == "none"
    assert result.items == []
    assert result.reranked is False
    assert result.unmatched_reason is not None


async def test_region_matcher_failure_also_degrades(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """地区匹配器也不可用时同样降级：两者都是检索路径上的外部依赖。"""

    class _BrokenMatcher:
        def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
            raise ValueError("地区表不可用")

    gateway = _FakeGateway([_rewrite_response("叶片")])
    agent = RetrieverAgent(cast(ModelGateway, gateway), _FakeKnowledge({}), _BrokenMatcher())

    result = await agent.run(_state(), ctx)

    assert result.hit is False
    assert result.region_level_actual == "none"
    assert result.unmatched_reason is not None


# ---- 第七组：mock 模式与零网络请求 ----


async def test_run_under_mock_returns_contract_object(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """`USE_MOCK_LLM=true` 下 `run` 返回满足契约的对象，证据仍来自检索结果。

    改写走的是真实 mock 网关：响应内容不是检索用词，改写结果因此在关键词里去重后
    只留下原词——这条正说明"改写只做增量、绝不替换"。
    """
    agent = RetrieverAgent(build_gateway(), _FakeKnowledge({"330102": [_item()]}), _FakeRegionMatcher())

    result = await agent.run(_state(), ctx)

    assert isinstance(result, EvidenceSet)
    assert result.hit is True
    assert result.query.process_id is ProcessId.TEA
    assert "叶发病率" in result.query.keywords


async def test_no_network_request_during_run(
    tea_config: ProcessConfig, ctx: AgentContext, respx_mock: respx.Router
) -> None:
    """证据核查全程不得发起任何 HTTP 请求：检索与改写都经注入的接口。"""
    agent, gateway, _, _ = _agent([_rewrite_response("茶饼病")], knowledge={"330102": [_item()]})

    await agent.run(_state(), ctx)

    assert len(gateway.calls) == 1
    assert respx_mock.calls == []
