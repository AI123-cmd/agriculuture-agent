"""处方智能体：把已核准的知识条目与判定结论组装为 `Prescription`（T2-21）。

本智能体的任务本质是"组装加改写"：**事实由代码从知识库检索结果填充，模型只负责把话说顺**。
三条底线在这里由代码保证，而不是靠提示词：

- **药剂条目只能来自确定性检索**：`fetch_pesticide_candidates` 在本流程内按地区回退链取
  `kind="pesticide"` 的已核准条目，逐字段照抄成 `PesticideItem`。模型无权重写这份清单——
  `post_validate` 用候选集强制覆写模型返回的 `pesticide_items`，模型只能"保留哪些、删掉哪些"，
  不能新增或改数值。
- **每个数值都登记来源**：`build_provenance` 生成 `knowledge:<id>:field:<name>` 形式的字段级
  溯源，键与 `Prescription.numeric_paths()` 一一对应。溯源完全由代码重建，不读模型输出，
  因此"登记表"与"实际数值"必然一致。
- **候选为空时置 `requires_tech_station=True`**：编造一条药剂比诚实地说"建议咨询农技站"危险
  得多。降级出口也只给求助引导，`pesticide_items=[]`。

油菜流程是唯一的例外：`fetch_pesticide_candidates` 直接返回空列表，实现技术方案 9.5 的
"把 L3 处置知识的检索开关设为 disabled"。检索层不放行比红线拦截更早生效，红线作为第二道
防线保留。

与其他智能体一致，提示词构造留在本模块内（公开的 `build_prescriber_prompt` 加若干私有
`_format_*` 纯函数），由 T2-60 统一迁移到模板目录。

`max_attempts = 1` 与其他主链路智能体不同：本智能体的模型调用只做话术改写，事实部分已在
代码里定好，一次改写失败（如 JSON 格式错）重试收益很低，直接降级为求助引导更快。
"""

import json
from typing import Any, cast

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.agents.followup_builder import build_followup_plan
from app.agent.agents.retriever import KnowledgeRetriever, RegionMatcher
from app.agent.blackboard import slot_summary
from app.agent.contracts import DiagnosisResult, EvidenceItem, EvidenceQuery, PesticideItem, Prescription
from app.agent.contracts.evidence import EvidenceKindValue
from app.agent.contracts.prescription import PrescriptionAction
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.llm import ModelGateway
from app.process_config.models import ProcessConfig, RedlineSpec

# 药剂候选的检索条数。与 T2-18 的常规 `top_k` 分开：处方只需要少数几个可替代的药剂，
# 候选过多会让模型在改写时把清单说成"以下多种药剂"，而农户需要的是明确的一两条。
_TOP_K_PESTICIDE = 6

# 候选清单里字段的展示顺序与单位写法。集中在这里，`_format_candidates` 与提示词里的
# "不可增删"约束用同一份清单，避免两处漂移。
_DOSAGE_UNIT_FALLBACK = "按标签标注用量"

# 降级处方的固定文案。`actions` 只有一条"到农技站咨询"，不含任何具体药剂与剂量，
# 与安全闸门 `degrade` 后的输出形态一致。
_DEGRADED_SUMMARY = "当前无法给出完整处置建议，建议咨询当地农技站。"
_DEGRADED_STOP_CONDITIONS = ["症状明显加重时立即联系农技人员"]


def build_prescriber_prompt(
    config: ProcessConfig,
    state: ConsultationState,
    candidates: list[PesticideItem],
) -> list[dict]:
    """拼装处方提示词。system 负责角色与硬规则，user 注入判定结论、候选与已确认信息。

    两条"硬规则"（不得增删药剂条目、不得改动数字）是提示词层面的约束，真正生效的是
    `post_validate` 的强制覆写。写进提示词是为了减少模型的无效尝试——它知道数字不可改，
    就会把注意力放在把话说顺上，而不是先编一版数字再被覆盖。

    注入 `section_order` / `max_chars` / `required_sections` 而非在代码里截断：输出模板是
    "一份配置驱动三处行为"中处方这一处的落点，字数与必含小节随流程变化。
    """
    template = config.output_template
    system = [
        f"你是{config.role_definition}，负责把已确定的事实组织成农户能看懂的处方。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(Prescription.model_json_schema(), ensure_ascii=False)}",
        "硬规则：不得新增、修改或删除任何药剂条目。",
        "硬规则：不得改动任何数字。数字只能原样使用下面给出的值。",
        f"输出段落顺序：{'、'.join(template.section_order)}。",
        f"总字数不超过 {template.max_chars} 字。",
        f"必含内容：{'、'.join(template.required_sections) or '无'}。",
        f"面向农户的表述：{_format_colloquial(config.terminology.colloquial_map)}",
        f"严禁输出：{_format_redlines(config.redlines)}",
    ]
    user = [
        f"判定结论：{_format_diagnosis(state.get('diagnosis'))}",
        f"可用药剂候选（不可增删）：{_format_candidates(candidates)}",
        f"已确认信息：{_format_slots(config, state)}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


def _format_colloquial(mapping: dict[str, str]) -> str:
    """内部术语 → 农户说法。展示层的替换表在这里作为表述约束注入。"""
    return "；".join(f"{term}说成「{spoken}」" for term, spoken in mapping.items()) or "无"


def _format_redlines(redlines: list[RedlineSpec]) -> str:
    """流程红线。只给编号与说明：规则细节属闸门职责，此处的作用是让模型不做无谓尝试。"""
    return "；".join(f"{item.id} {item.desc}" for item in redlines) or "无"


def _format_diagnosis(diagnosis: object) -> str:
    """判定结论摘要。state 里存的是 `DiagnosisResult` 序列化后的字典。"""
    if not isinstance(diagnosis, dict) or not diagnosis:
        return "无（判定未产出结论）"
    parts = [f"结论={diagnosis.get('conclusion') or '未知'}"]
    level = diagnosis.get("confidence_level")
    if level:
        parts.append(f"把握程度={level}")
    basis = diagnosis.get("basis")
    if isinstance(basis, list) and basis:
        parts.append("依据：" + "；".join(str(item) for item in basis))
    return "；".join(parts)


def _format_slots(config: ProcessConfig, state: ConsultationState) -> str:
    """已确认与"答不出"的槽位。用口语标签而不是槽位键：键名对模型是噪声。"""
    summary = slot_summary(state)
    if not summary:
        return "无"
    parts: list[str] = []
    for key, value in summary.items():
        spec = config.slot_spec(key)
        parts.append(f"{spec.display_label if spec else key}={value}")
    return "；".join(parts)


def _format_candidates(candidates: list[PesticideItem]) -> str:
    """候选药剂清单，逐项给出全部事实字段。

    字段必须一起给出：只给药剂名，模型为了"说得完整"就会自己补一个剂量。给全剂量、
    间隔期与每季上限之后，"照抄"是它唯一能做的事。
    """
    if not candidates:
        return "无（本流程不提供药剂候选，请只给观察与求助建议）"
    lines: list[str] = []
    for item in candidates:
        dosage = _format_dosage(item)
        extras = [
            f"安全间隔期 {item.safety_interval_days} 天" if item.safety_interval_days is not None else "",
            f"每季最多 {item.max_uses_per_season} 次" if item.max_uses_per_season is not None else "",
            f"登记作物 {'、'.join(item.registered_crops)}" if item.registered_crops else "",
            f"禁忌搭配 {'、'.join(item.forbidden_combinations)}" if item.forbidden_combinations else "",
        ]
        detail = "，".join(part for part in extras if part)
        lines.append(f"- {item.name}（条目 {item.knowledge_id}）：{dosage}{'，' + detail if detail else ''}")
    return "\n".join(lines)


def _format_dosage(item: PesticideItem) -> str:
    """剂量区间的可读写法。两端都缺时给一句"按标签标注用量"而不是空串。"""
    unit = item.dosage_unit or ""
    if item.dosage_min is not None and item.dosage_max is not None:
        return f"用量 {item.dosage_min}~{item.dosage_max} {unit}".strip()
    if item.dosage_min is not None:
        return f"用量不低于 {item.dosage_min} {unit}".strip()
    if item.dosage_max is not None:
        return f"用量不高于 {item.dosage_max} {unit}".strip()
    return _DOSAGE_UNIT_FALLBACK


class PrescriberAgent(BaseAgent[Prescription]):
    """处方：确定性检索药剂候选、模型只改写、代码强制覆写事实字段。"""

    name = "prescriber"
    output_schema = Prescription
    task_type = TaskType.PRESCRIBE
    failure_mode = FailureMode.DEGRADE  # 转为求助引导
    # 改写任务，重试收益低。注意基类的 `max_attempts` 语义是"首次之外的额外次数"，
    # 故总尝试次数为 1 + 1 = 2。
    max_attempts = 1

    def __init__(
        self,
        gateway: ModelGateway,
        knowledge: KnowledgeRetriever,
        region_matcher: RegionMatcher,
    ) -> None:
        super().__init__(gateway)
        self._knowledge = knowledge
        self._region = region_matcher

    async def run(self, state: ConsultationState, ctx: AgentContext) -> Prescription:
        """先做确定性检索，再走基类的调用、覆写与降级。

        候选在 `run` 里算而不是在 `build_messages` 里：后者会被重试反复调用，而检索是
        有 IO 成本的一步。结果写进 state 的临时键 `_pesticide_candidates`（下划线前缀表示
        不属于状态契约），供 `build_messages` 与 `post_validate` 读取同一份候选——两者若
        各自检索一次，模型的输入与代码的覆写依据可能来自不同批次，溯源就会对不上。
        """
        try:
            candidates = await self.fetch_pesticide_candidates(state, ctx)
            cast(dict[str, Any], state)["_pesticide_candidates"] = candidates
        except Exception as exc:
            # 检索故障与模型故障走同一个降级出口：两者都导向"求助引导处方"。
            return await self._on_failure(state, ctx, exc)
        return await super().run(state, ctx)

    # ---- 确定性检索 ----

    async def fetch_pesticide_candidates(self, state: ConsultationState, ctx: AgentContext) -> list[PesticideItem]:
        """确定性检索：依据流程、判定结论、地区、用药史取候选条目。

        油菜流程直接返回空列表（技术方案 9.5 的 L3 检索开关 `disabled`）：检索层不放行比
        红线拦截更早生效，红线作为第二道防线。
        """
        config = ctx.process_config
        raw_diagnosis = state.get("diagnosis")
        if not raw_diagnosis:
            return []
        diagnosis = DiagnosisResult.model_validate(raw_diagnosis)
        if config.process_id is ProcessId.RAPESEED:
            return []
        query = EvidenceQuery(
            process_id=config.process_id,
            keywords=[diagnosis.conclusion, *diagnosis.basis[:2]],
            region_code=ctx.region_code,
            slot_snapshot=slot_summary(state),
            kind_filter=cast(list[EvidenceKindValue], ["pesticide"]),
            top_k=_TOP_K_PESTICIDE,
        )
        chain = self._region.build_chain(ctx.region_code)
        items: list[EvidenceItem] = []
        for _, code in chain:
            items = await self._knowledge.search(
                query.model_copy(update={"region_code": code}),
                region_chain=[c for _, c in chain],
            )
            if items:
                break
        return [self._to_pesticide_item(item) for item in items if self._passes_history(item, state)]

    def _to_pesticide_item(self, item: EvidenceItem) -> PesticideItem:
        """知识条目到药剂条目的映射。数值全部照抄，不改写。

        `name` 取 `title` 而不是 `content`：条目的标题就是药剂名，正文是说明与注意事项，
        把它塞进药剂名会让处方出现"示例药剂A：适用于……"这类以说明当名称的排版故障。
        """
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

    def _passes_history(self, item: EvidenceItem, state: ConsultationState) -> bool:
        """按档案用药史剔除重复用药与禁忌搭配。

        这是 `R-CONFLICT` 的输入侧预过滤，闸门仍会二次校验——两道防线并存是因为用药史
        快照的字段形态由档案模型决定（不保证有统一键名），匹配只能按值做包含判断，
        精度有限，不能作为唯一把关点。

        历史条目形态不固定，因此把所有字符串值摊平成一个集合再比对：条目名出现在历史里
        视为重复用药，条目的禁忌搭配出现在历史里视为配伍禁忌。
        """
        history_terms = _history_terms(state.get("prescription_history"))
        if not history_terms:
            return True
        if item.title in history_terms:
            return False
        return not any(term in history_terms for term in item.forbidden_combinations)

    # ---- 提示词与强制覆写 ----

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词。候选来自本轮 `run` 的检索结果。"""
        candidates = cast(list[PesticideItem], cast(dict[str, Any], state).get("_pesticide_candidates") or [])
        return build_prescriber_prompt(ctx.process_config, state, candidates)

    def build_provenance(self, items: list[PesticideItem]) -> dict[str, str]:
        """为处方中每个数值登记来源。键为字段路径。

        来源标识精确到字段（`knowledge:<id>:field:<name>`），让闸门的反向扫描能回答
        "这个数字具体对应条目里的哪个字段"，而不是只知道它来自某个条目。

        顶层两个数值同样要登记：`numeric_paths()` 把它们算作需要溯源的路径，而它们又是
        `conservative_top_level` 从候选里挑出来的，因此出处就是提供该值的那个条目。登记
        与取值在本方法内一起算，两者不可能漂移——若顶层值有出处而登记表漏了它，契约的
        正向核对会当场失败。
        """
        prov: dict[str, str] = {}
        for i, item in enumerate(items):
            base = f"knowledge:{item.knowledge_id}"
            for field in ("dosage_min", "dosage_max", "safety_interval_days", "max_uses_per_season"):
                if getattr(item, field) is not None:
                    prov[f"pesticide_items[{i}].{field}"] = f"{base}:field:{field}"
        top_interval, top_uses = self.conservative_top_level(items)
        for path, value, field in (
            ("safety_interval_days", top_interval, "safety_interval_days"),
            ("max_uses_per_season", top_uses, "max_uses_per_season"),
        ):
            if value is None:
                continue
            source = next(
                (item for item in items if getattr(item, field) == value),
                None,
            )
            if source is not None:
                prov[path] = f"knowledge:{source.knowledge_id}:field:{field}"
        return prov

    def conservative_top_level(self, items: list[PesticideItem]) -> tuple[int | None, int | None]:
        """顶层安全间隔期与每季次数取候选中最保守者（间隔最长、次数最少）。

        候选有多个时展示最保守的一个：把间隔期报短会让农户在药残未降解时采收，
        报长只会让他多等几天；每季次数同理，报少比报多安全。
        """
        intervals = [i.safety_interval_days for i in items if i.safety_interval_days is not None]
        uses = [i.max_uses_per_season for i in items if i.max_uses_per_season is not None]
        return (max(intervals) if intervals else None, min(uses) if uses else None)

    async def post_validate(self, result: Prescription, state: ConsultationState, ctx: AgentContext) -> Prescription:
        """用代码的结果强制覆写模型可能篡改的部分。

        三道覆写各有针对性：

        1. `pesticide_items` 被换成候选集的子集，因此剂量与间隔期不可能是模型编的；
           `keep_ids` 为空时退回全部候选，避免模型写错 id 导致处方无药剂可给。
        2. 顶层数值取最保守值，不接受模型给的值。
        3. `facts_provenance` 完全由代码重建，使溯源与实际数值必然一致。
        """
        candidates = cast(list[PesticideItem], cast(dict[str, Any], state).get("_pesticide_candidates") or [])
        keep_ids = {item.knowledge_id for item in result.pesticide_items}
        items = [c for c in candidates if c.knowledge_id in keep_ids] or candidates
        top_interval, top_uses = self.conservative_top_level(items)
        merged = {
            **result.model_dump(),
            "pesticide_items": [i.model_dump() for i in items],
            "safety_interval_days": top_interval,
            "max_uses_per_season": top_uses,
            "facts_provenance": self.build_provenance(items),
            "requires_tech_station": result.requires_tech_station or not items,
        }
        return Prescription.model_validate(merged)

    # ---- 改动检测 ----

    def detect_tampering(self, result: Prescription, candidates: list[PesticideItem]) -> list[str]:
        """检测模型是否试图改动事实。覆写已保证不生效，此处仅为可观测。

        这条指标（模型试图改动事实的比例）是判断提示词质量与是否需要换模型的直接依据，
        也是技术方案 15 风险表里"模型改写事实性内容"的监控手段。
        """
        allowed = {c.knowledge_id: c for c in candidates}
        findings: list[str] = []
        for item in result.pesticide_items:
            origin = allowed.get(item.knowledge_id)
            if origin is None:
                findings.append(f"新增未知条目 {item.knowledge_id}")
                continue
            for field in ("dosage_min", "dosage_max", "safety_interval_days", "max_uses_per_season"):
                if getattr(item, field) != getattr(origin, field):
                    findings.append(f"{item.knowledge_id}.{field} 被改动")
        return findings

    # ---- 降级 ----

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> Prescription:
        """降级：转为求助引导，不输出任何药剂。

        降级处方必须能通过闸门校验，否则会出现"降级后又降级"的链路。`facts_provenance={}`
        之所以能通过，是因为没有数值字段，`numeric_paths()` 返回空列表。
        """
        return Prescription(
            diagnosis_summary=_DEGRADED_SUMMARY,
            actions=[
                PrescriptionAction(
                    order=1,
                    action="到当地农技站咨询",
                    timing="尽快",
                    method="携带症状照片与本次问诊记录",
                    cautions=["不要自行加大用药量"],
                    stop_conditions=list(_DEGRADED_STOP_CONDITIONS),
                )
            ],
            safety_interval_days=None,
            max_uses_per_season=None,
            pesticide_items=[],
            soil_level_actions=[],
            followup_plan=build_followup_plan(ctx.process_config.process_id, None),
            facts_provenance={},
            requires_tech_station=True,
            detail_terms={},
        )


def _history_terms(history: object) -> set[str]:
    """把档案用药史摊平成字符串集合。字段形态由档案快照决定，这里不假设具体键名。"""
    terms: set[str] = set()
    if not isinstance(history, list):
        return terms
    for row in history:
        if isinstance(row, dict):
            for value in row.values():
                if isinstance(value, str) and value.strip():
                    terms.add(value.strip())
                elif isinstance(value, list):
                    terms.update(str(v).strip() for v in value if str(v).strip())
        elif isinstance(row, str) and row.strip():
            terms.add(row.strip())
    return terms


__all__ = ["PrescriberAgent", "build_prescriber_prompt"]
