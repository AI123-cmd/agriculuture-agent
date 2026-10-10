"""T2-21：处方智能体的来源约束、溯源登记与空候选处理。

用例按七组组织：

1. 确定性检索：候选来自 `kind="pesticide"` 的检索结果并逐字段照抄；油菜流程直接返回空
   列表且**不调用检索**（L3 检索开关 disabled）；地区逐级回退到命中即止；档案用药史剔除
   重复用药与禁忌搭配；
2. 模型越界被覆写：模型返回候选之外的 `knowledge_id` 时被丢弃并退回候选集；模型篡改
   `dosage_max` 时实际输出仍为候选原值；顶层间隔期与每季次数按最保守值覆写；
3. 溯源登记：`facts_provenance` 的键集合等于 `Prescription.numeric_paths()`，值形如
   `knowledge:<id>:field:<name>`；
4. 空候选：`requires_tech_station=True` 且 `pesticide_items=[]`，处方仍满足契约；
5. 改动检测：`detect_tampering` 能识别新增未知条目与改动数值两类；
6. 回访计划：药害分支单任务且 `due_offset_days=0`、`kind="preserve_scene"`；辣椒两条
   任务为 3 与 7 天；
7. 降级与 mock：降级处方可被 `model_validate` 通过且 `numeric_paths()` 为空；全程零 HTTP。

网关、知识库与地区匹配器都是替身：本智能体在这三者面前是**被调用方**。流程配置取真实的
辣椒、油菜与茶叶流程。
"""

import json
from datetime import date
from typing import Any, cast

import pytest
import respx

from app.agent.agents.base import AgentContext
from app.agent.agents.followup_builder import HERBICIDE_DAMAGE_BRANCH, build_followup_plan
from app.agent.agents.prescriber import PrescriberAgent
from app.agent.contracts import DiagnosisResult, EvidenceItem, EvidenceQuery, PesticideItem, Prescription
from app.agent.prompts.prescriber import build_prescriber_prompt
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.llm import ModelGateway, build_gateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

_MESSAGE = "我家辣椒突然蔫了，叶子还是绿的"
_CONCLUSION = "青枯病"
_CHAIN = [("county", "330102"), ("city", "330100"), ("province", "330000"), ("national", "000000")]


def _item(**overrides: Any) -> EvidenceItem:
    """一条辣椒流程的药剂条目。数值字段按 T2-49 的 `_to_evidence` 形态给出。"""
    data: dict[str, Any] = {
        "knowledge_id": "kb_chili_pest_001",
        "title": "示例药剂A",
        "content": "发病初期使用，重点喷淋茎基部。",
        "process_id": "chili",
        "region_scope": "county:330102",
        "effective_date": date(2025, 3, 1),
        "kind": "pesticide",
        "score": 0.9,
        "dosage_min": 15.0,
        "dosage_max": 20.0,
        "dosage_unit": "ml/亩",
        "safety_interval_days": 7,
        "max_uses_per_season": 2,
        "registered_crops": ["辣椒"],
        "forbidden_combinations": ["碱性农药"],
    }
    data.update(overrides)
    return EvidenceItem(**data)


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
    """知识库检索接口的替身：按地区编码回放条目，或整体抛一个异常。"""

    def __init__(self, by_region: dict[str, list[EvidenceItem]] | Exception) -> None:
        self._by_region = by_region
        self.calls: list[tuple[str | None, str]] = []
        self.kinds: list[list[str]] = []

    async def search(self, query: EvidenceQuery, region_chain: list[str]) -> list[EvidenceItem]:
        self.calls.append((query.region_code, query.process_id.value))
        self.kinds.append(list(query.kind_filter))
        if isinstance(self._by_region, Exception):
            raise self._by_region
        return list(self._by_region.get(query.region_code or "", []))


class _FakeRegionMatcher:
    """地区回退链构造接口的替身。"""

    def __init__(self, chain: list[tuple[str, str]] | None = None) -> None:
        self._chain = list(chain if chain is not None else _CHAIN)

    def build_chain(self, region_code: str | None) -> list[tuple[str, str]]:
        return list(self._chain)


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(scope="module")
def chili_config() -> ProcessConfig:
    return get_process_config(ProcessId.CHILI)


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
        task_type=TaskType.PRESCRIBE,
        trace_id="t-1",
        region_code="330102",
    )


@pytest.fixture()
def ctx(chili_config: ProcessConfig) -> AgentContext:
    return _ctx(chili_config)


def _agent(
    results: list[ModelResponse | Exception],
    by_region: dict[str, list[EvidenceItem]] | Exception | None = None,
) -> tuple[PrescriberAgent, _FakeGateway, _FakeKnowledge]:
    gateway = _FakeGateway(results)
    knowledge = _FakeKnowledge(by_region if by_region is not None else {"330102": [_item()]})
    agent = PrescriberAgent(
        cast(ModelGateway, gateway),
        knowledge,
        _FakeRegionMatcher(),
    )
    return agent, gateway, knowledge


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _MESSAGE}],
        "diagnosis": {
            "conclusion": _CONCLUSION,
            "confidence_level": "preliminary",
            "basis": ["挤压切口渗出乳白色菌脓", "维管束黄褐色"],
            "uncertainty_source": [],
            "excluded": ["枯萎病"],
            "terminology_profile": "chili",
            "needs_more_info": [],
            "out_of_scope": False,
            "boundary_reason": None,
        },
    }
    state.update(overrides)
    return state


def _provenance(items: list[PesticideItem]) -> dict[str, str]:
    """与 `build_provenance` 同规则：药剂字段路径 + 顶层两个最保守数值的路径。"""
    prov: dict[str, str] = {}
    for i, item in enumerate(items):
        for field in ("dosage_min", "dosage_max", "safety_interval_days", "max_uses_per_season"):
            if getattr(item, field) is not None:
                prov[f"pesticide_items[{i}].{field}"] = f"knowledge:{item.knowledge_id}:field:{field}"
    intervals = [(i.safety_interval_days, i) for i in items if i.safety_interval_days is not None]
    uses = [(i.max_uses_per_season, i) for i in items if i.max_uses_per_season is not None]
    if intervals:
        prov["safety_interval_days"] = (
            f"knowledge:{max(intervals, key=lambda pair: pair[0])[1].knowledge_id}:field:safety_interval_days"
        )
    if uses:
        prov["max_uses_per_season"] = (
            f"knowledge:{min(uses, key=lambda pair: pair[0])[1].knowledge_id}:field:max_uses_per_season"
        )
    return prov


def _payload(items: list[PesticideItem], **overrides: Any) -> dict[str, Any]:
    """模型侧的最小合法输出。`facts_provenance` 覆盖 `numeric_paths()` 的全部路径。"""
    intervals = [i.safety_interval_days for i in items if i.safety_interval_days is not None]
    uses = [i.max_uses_per_season for i in items if i.max_uses_per_season is not None]
    data: dict[str, Any] = {
        "diagnosis_summary": "辣椒青枯病，发病初期",
        "actions": [
            {
                "order": 1,
                "action": "喷淋茎基部",
                "timing": "晴天傍晚",
                "method": "叶面均匀喷雾，重点覆盖茎基部",
                "cautions": ["避免高温时段施药"],
                "stop_conditions": ["出现药害斑立即停用并联系农技站"],
            }
        ],
        "safety_interval_days": max(intervals) if intervals else None,
        "max_uses_per_season": min(uses) if uses else None,
        "pesticide_items": [i.model_dump() for i in items],
        "soil_level_actions": [],
        "followup_plan": {"tasks": [], "note": None},
        "facts_provenance": _provenance(items),
        "requires_tech_station": False,
        "detail_terms": {},
    }
    data.update(overrides)
    return data


def _ok(items: list[PesticideItem] | None = None, **overrides: Any) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象，不是裸字典。"""
    data = _payload(items if items is not None else [_item_to_pesticide(_item())], **overrides)
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=Prescription.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


def _item_to_pesticide(item: EvidenceItem) -> PesticideItem:
    """测试侧的知识条目 → 药剂条目映射，与 `_to_pesticide_item` 同规则。"""
    return PesticideItem(
        knowledge_id=item.knowledge_id,
        name=item.title,
        dosage_min=item.dosage_min,
        dosage_max=item.dosage_max,
        dosage_unit=item.dosage_unit,
        safety_interval_days=item.safety_interval_days,
        max_uses_per_season=item.max_uses_per_season,
        registered_crops=list(item.registered_crops),
        forbidden_combinations=list(item.forbidden_combinations),
        source_note=f"knowledge:{item.knowledge_id}",
    )


def _parse_failure() -> ModelResponse:
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


# ---- 第一组：确定性检索 ----


async def test_candidates_copied_field_by_field(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """候选逐字段照抄知识条目，不做换算或推断——这是"模板出事实"。"""
    agent, _, _ = _agent([])

    candidates = await agent.fetch_pesticide_candidates(_state(), ctx)

    assert len(candidates) == 1
    item = candidates[0]
    assert item.knowledge_id == "kb_chili_pest_001"
    assert item.name == "示例药剂A"  # 取 title，不是 content
    assert (item.dosage_min, item.dosage_max, item.dosage_unit) == (15.0, 20.0, "ml/亩")
    assert item.safety_interval_days == 7
    assert item.max_uses_per_season == 2
    assert item.registered_crops == ["辣椒"]
    assert item.forbidden_combinations == ["碱性农药"]
    assert item.source_note == "knowledge:kb_chili_pest_001"


async def test_candidates_query_only_asks_pesticide(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """检索只取 `kind="pesticide"` 的条目，且 `process_id` 为本流程。"""
    agent, _, knowledge = _agent([])

    await agent.fetch_pesticide_candidates(_state(), ctx)

    assert knowledge.kinds == [["pesticide"]]
    assert knowledge.calls[0][1] == "chili"


async def test_rapeseed_returns_empty_without_search(rapeseed_config: ProcessConfig) -> None:
    """油菜流程直接返回空列表，且**不调用检索**：L3 处置知识开关 disabled 落在检索层。"""
    agent, _, knowledge = _agent([])

    candidates = await agent.fetch_pesticide_candidates(_state(), _ctx(rapeseed_config))

    assert candidates == []
    assert knowledge.calls == []


async def test_region_fallback_stops_at_first_hit(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """逐级回退，县级命中即停止：继续往下取只会引入更泛化的条目。"""
    agent, _, knowledge = _agent([], by_region={"330100": [_item(knowledge_id="kb_city")]})

    candidates = await agent.fetch_pesticide_candidates(_state(), ctx)

    assert [c.knowledge_id for c in candidates] == ["kb_city"]
    assert [code for code, _ in knowledge.calls] == ["330102", "330100"]


async def test_history_drops_repeated_and_forbidden(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """档案用药史剔除重复用药与禁忌搭配：`R-CONFLICT` 的输入侧预过滤。"""
    agent, _, _ = _agent([])
    repeated = _state(prescription_history=[{"product": "示例药剂A"}])
    forbidden = _state(prescription_history=[{"product": "另一个药", "mix": ["碱性农药"]}])

    assert await agent.fetch_pesticide_candidates(repeated, ctx) == []
    assert await agent.fetch_pesticide_candidates(forbidden, ctx) == []
    assert len(await agent.fetch_pesticide_candidates(_state(prescription_history=[{"product": "别的药"}]), ctx)) == 1


async def test_no_diagnosis_yields_no_candidates(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """判定还没产出结论时不检索：没有结论就没有检索关键词。"""
    agent, _, knowledge = _agent([])

    assert await agent.fetch_pesticide_candidates(_state(diagnosis=None), ctx) == []
    assert knowledge.calls == []


# ---- 第二组：模型越界被覆写 ----


async def test_unknown_knowledge_id_falls_back_to_candidates(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型返回候选之外的条目时被丢弃，并退回全部候选（避免处方无药剂）。"""
    invented = PesticideItem(knowledge_id="kb_fake_999", name="编造药剂", dosage_min=1.0)
    agent, _, _ = _agent([_ok([invented])])

    result = await agent.run(_state(), ctx)

    assert [i.knowledge_id for i in result.pesticide_items] == ["kb_chili_pest_001"]


async def test_tampered_dosage_is_overwritten(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型篡改 `dosage_max` 时实际输出仍为候选原值。"""
    tampered = _item_to_pesticide(_item())
    tampered.dosage_max = 999.0
    agent, _, _ = _agent([_ok([tampered])])

    result = await agent.run(_state(), ctx)

    assert result.pesticide_items[0].dosage_max == 20.0


async def test_top_level_is_most_conservative(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """顶层数值取最保守者，不接受模型给的值。"""
    agent, _, _ = _agent([_ok([_item_to_pesticide(_item(safety_interval_days=7, max_uses_per_season=2))])])

    result = await agent.run(
        _state(),
        ctx,
    )

    assert result.safety_interval_days == 7
    assert result.max_uses_per_season == 2


def test_conservative_top_level_picks_longest_interval(chili_config: ProcessConfig) -> None:
    """候选间隔期为 [7, 10] 时返回 10：报短会让农户在药残未降解时采收。"""
    agent, _, _ = _agent([])

    interval, uses = agent.conservative_top_level(
        [
            PesticideItem(knowledge_id="a", name="A", safety_interval_days=7, max_uses_per_season=3),
            PesticideItem(knowledge_id="b", name="B", safety_interval_days=10, max_uses_per_season=1),
        ]
    )

    assert interval == 10
    assert uses == 1


def test_conservative_top_level_without_numbers(chili_config: ProcessConfig) -> None:
    """一条数值都没有时返回 `(None, None)`，不编造默认值。"""
    agent, _, _ = _agent([])

    assert agent.conservative_top_level([PesticideItem(knowledge_id="a", name="A")]) == (None, None)
    assert agent.conservative_top_level([]) == (None, None)


# ---- 第三组：溯源登记 ----


async def test_provenance_keys_equal_numeric_paths(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """`facts_provenance` 的键集合等于 `numeric_paths()`：正向核对必然通过。"""
    agent, _, _ = _agent(
        [_ok([_item_to_pesticide(_item()), _item_to_pesticide(_item(knowledge_id="kb_2", dosage_max=None))])]
    )

    result = await agent.run(_state(), ctx)

    assert set(result.facts_provenance) == set(result.numeric_paths())
    assert "pesticide_items[0].dosage_min" in result.facts_provenance
    assert result.facts_provenance["pesticide_items[0].dosage_min"] == ("knowledge:kb_chili_pest_001:field:dosage_min")


def test_provenance_skips_missing_numbers(chili_config: ProcessConfig) -> None:
    """取值为 `None` 的字段不进溯源表：没有数字就没有需要登记的对象。"""
    agent, _, _ = _agent([])

    prov = agent.build_provenance([PesticideItem(knowledge_id="kb_1", name="A", dosage_min=5.0)])

    assert prov == {"pesticide_items[0].dosage_min": "knowledge:kb_1:field:dosage_min"}


# ---- 第四组：空候选 ----


async def test_empty_candidates_requires_tech_station(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """候选为空时置 `requires_tech_station=True` 且不给任何药剂，而不是编造条目。"""
    agent, _, _ = _agent([_ok([])], by_region={})

    result = await agent.run(_state(), ctx)

    assert result.pesticide_items == []
    assert result.requires_tech_station is True
    assert result.safety_interval_days is None
    assert result.facts_provenance == {}


async def test_requires_tech_station_kept_when_model_sets_it(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """模型主动要求转农技站时保留该判断：覆写只做"或"运算，不抹掉模型的保守选择。"""
    agent, _, _ = _agent([_ok(requires_tech_station=True)])

    result = await agent.run(_state(), ctx)

    assert result.requires_tech_station is True
    assert len(result.pesticide_items) == 1


# ---- 第五组：改动检测 ----


def test_detect_tampering_reports_invented_item(chili_config: ProcessConfig) -> None:
    """新增未知条目被识别。覆写已保证不生效，此处为可观测指标。"""
    agent, _, _ = _agent([])
    candidates = [_item_to_pesticide(_item())]
    result = Prescription.model_validate(_payload([PesticideItem(knowledge_id="kb_fake", name="编造")]))

    findings = agent.detect_tampering(result, candidates)

    assert findings == ["新增未知条目 kb_fake"]


def test_detect_tampering_reports_changed_number(chili_config: ProcessConfig) -> None:
    """改动数值被识别，报告里带条目与字段名。"""
    agent, _, _ = _agent([])
    candidates = [_item_to_pesticide(_item())]
    tampered = _item_to_pesticide(_item())
    tampered.dosage_max = 999.0
    result = Prescription.model_validate(_payload([tampered]))

    findings = agent.detect_tampering(result, candidates)

    assert findings == ["kb_chili_pest_001.dosage_max 被改动"]


def test_detect_tampering_clean_output(chili_config: ProcessConfig) -> None:
    """模型照抄候选时不产生任何发现。"""
    agent, _, _ = _agent([])
    candidates = [_item_to_pesticide(_item())]

    assert agent.detect_tampering(Prescription.model_validate(_payload(candidates)), candidates) == []


# ---- 第六组：回访计划 ----


def test_herbicide_branch_single_immediate_task() -> None:
    """药害分支恰好一条任务，`due_offset_days=0` 且 `kind="preserve_scene"`。"""
    plan = build_followup_plan(ProcessId.WEED, HERBICIDE_DAMAGE_BRANCH)

    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.due_offset_days == 0
    assert task.kind == "preserve_scene"
    assert task.content_rule == "weed.damage_evidence_checklist"
    assert task.ask


def test_herbicide_branch_does_not_stack_regular_tasks() -> None:
    """药害分支下不叠加常规回访：不能既让农户保护现场又让他观察药效。"""
    plan = build_followup_plan(ProcessId.WEED, HERBICIDE_DAMAGE_BRANCH)

    assert [t.kind for t in plan.tasks] == ["preserve_scene"]


def test_chili_plan_has_single_task() -> None:
    """辣椒流程回访一条，药后 3 至 7 天取中位 5 天（T3-09 第一节）。"""
    plan = build_followup_plan(ProcessId.CHILI, None)

    assert [t.due_offset_days for t in plan.tasks] == [5]
    assert all(t.ask for t in plan.tasks)


def test_plan_window_based_follows_config() -> None:
    """`window_based` 来自流程配置，不在代码里硬编码。"""
    assert build_followup_plan(ProcessId.CHILI, None).tasks[0].window_based is False
    assert build_followup_plan(ProcessId.TEA, None).tasks[0].window_based is True


def test_pig_plan_keeps_compliance_check_kind() -> None:
    """养猪流程的法定时限链用 `compliance_check`，不得被折成 `effect_check`。"""
    plan = build_followup_plan(ProcessId.PIG, None)

    assert "compliance_check" in {t.kind for t in plan.tasks}


# ---- 第七组：降级、提示词与 mock ----


async def test_degrade_prescription_is_valid_and_has_no_numbers(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """降级处方可被 `model_validate` 通过，且 `numeric_paths()` 为空——不会出现"降级后又降级"。"""
    agent, _, _ = _agent([])

    result = await agent.degrade(_state(), ctx, "ValueError: 网关未装配")
    rebuilt = Prescription.model_validate(result.model_dump())

    assert rebuilt.numeric_paths() == []
    assert rebuilt.facts_provenance == {}
    assert rebuilt.pesticide_items == []
    assert rebuilt.requires_tech_station is True
    assert rebuilt.actions[0].action == "到当地农技站咨询"
    assert rebuilt.actions[0].stop_conditions


async def test_gateway_failure_degrades(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """网关不可用时降级为求助引导处方。"""
    agent, gateway, _ = _agent([ValueError("网关未装配")])

    result = await agent.run(_state(), ctx)

    assert len(gateway.calls) == 1
    assert result.requires_tech_station is True
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "prescriber", "mode": "degrade"}) == 1


async def test_retrieval_failure_degrades(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """检索故障与模型故障走同一降级出口：两者都导向求助引导。"""
    agent, gateway, _ = _agent([_ok()], by_region=RuntimeError("知识库不可用"))

    result = await agent.run(_state(), ctx)

    assert gateway.calls == []
    assert result.requires_tech_station is True
    assert result.pesticide_items == []


async def test_parse_failure_retries_then_degrades(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """解析失败重试一次仍失败即降级：`max_attempts=1` 表示首次之外还有一次。"""
    agent, gateway, _ = _agent([_parse_failure()])

    result = await agent.run(_state(), ctx)

    assert len(gateway.calls) == 2
    assert result.requires_tech_station is True


def test_prompt_injects_facts_and_hard_rules(chili_config: ProcessConfig) -> None:
    """提示词含角色、两条硬规则、输出模板与候选清单。"""
    messages = build_prescriber_prompt(chili_config, _state(), [_item_to_pesticide(_item())])
    system = messages[0]["content"]
    user = messages[1]["content"]

    assert [m["role"] for m in messages] == ["system", "user"]
    assert chili_config.role_definition in system
    assert "不得新增、修改或删除任何药剂条目" in system
    assert "不得改动任何数字" in system
    assert str(chili_config.output_template.max_chars) in system
    for section in chili_config.output_template.section_order:
        assert section in system
    assert "示例药剂A" in user


def test_prompt_marks_empty_candidates(chili_config: ProcessConfig) -> None:
    """候选为空时提示词写明只给观察与求助建议：模型才不会自己编一条药剂。"""
    user = build_prescriber_prompt(chili_config, _state(), [])[1]["content"]

    assert "无" in user
    assert "观察与求助建议" in user


def test_prompt_shows_dosage_for_candidate(chili_config: ProcessConfig) -> None:
    """候选清单给出完整剂量与间隔期：给全之后"照抄"是模型唯一能做的事。"""
    user = build_prescriber_prompt(chili_config, _state(), [_item_to_pesticide(_item())])[1]["content"]

    assert "15.0~20.0" in user
    assert "安全间隔期 7 天" in user
    assert "每季最多 2 次" in user


def test_messages_use_candidates_from_state(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """`build_messages` 读 state 上本轮写入的候选，与覆写依据是同一份。"""
    agent, _, _ = _agent([])
    state = _state(_pesticide_candidates=[_item_to_pesticide(_item())])

    assert "示例药剂A" in agent.build_messages(state, ctx)[1]["content"]


async def test_diagnosis_is_validated_from_state(chili_config: ProcessConfig, ctx: AgentContext) -> None:
    """判定结论按契约校验后使用：state 里的字典来自检查点，可能字段缺失。"""
    agent, gateway, _ = _agent([_ok()])

    result = await agent.run(_state(), ctx)

    assert len(gateway.calls) == 1
    assert result.pesticide_items
    # 判定结论进了提示词，模型据此改写话术。
    assert _CONCLUSION in gateway.last_messages[1]["content"]


async def test_run_under_mock_returns_contract_object(rapeseed_config: ProcessConfig) -> None:
    """`USE_MOCK_LLM=true` 下 `run` 返回满足契约的对象。"""
    agent, _, _ = _agent([], by_region={})
    agent = PrescriberAgent(
        build_gateway(),
        _FakeKnowledge({}),
        _FakeRegionMatcher(),
    )

    result = await agent.run(_state(), _ctx(rapeseed_config))

    assert isinstance(result, Prescription)
    assert result.pesticide_items == []


async def test_no_network_request_during_run(
    chili_config: ProcessConfig, ctx: AgentContext, respx_mock: respx.Router
) -> None:
    """处方全程不得发起任何 HTTP 请求：模型调用经注入的网关。"""
    agent, gateway, _ = _agent([_ok()])

    await agent.run(_state(), ctx)

    assert len(gateway.calls) == 1
    assert respx_mock.calls == []
