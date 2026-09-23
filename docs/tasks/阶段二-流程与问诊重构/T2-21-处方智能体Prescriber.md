# T2-21 处方智能体 Prescriber

## 这一项要做什么

在 `app/agent/agents/prescriber.py` 中实现处方智能体，把已核准的知识条目与判定结论组装为 `Prescription`。它的任务本质是"组装加改写"：事实由代码从知识库检索结果填充，模型只负责把话说顺，`pesticide_items` 列表模型无权写入。 本智能体要保证三件事：`pesticide_items` 只能来自确定性检索、每个数值在 `facts_provenance` 中登记来源、候选为空时置 `requires_tech_station=True` 而不是编造条目。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`AgentContext`；依赖 T2-50：必须已提供地区回退匹配（处方检索与证据检索共用）
- 依赖 T2-11：必须已提供 `Prescription`、`PesticideItem`、`FollowupPlan`；依赖 T2-19：必须已提供 `DiagnosisResult`，处方以其结论为输入
- 依赖 T2-18：必须已提供 `EvidenceSet`，药剂候选从其中的 `kind="pesticide"` 条目派生；依赖 T2-02：必须已提供 `ProcessConfig` 的 `output_template` 与 `followup_policy`
- 外部条件：药剂清单、剂量区间、安全间隔期需人工确认

## 参考章节

- 技术方案 第5章 5.3.5 处方契约 Prescription（`facts_provenance` 的核心机制）；技术方案 第5章 5.3.5 `pesticide_items` 的来源约束（只能由代码填充，候选为空时的处理）
- 技术方案 第5章 5.1 智能体总表（⑥处方，自治程度低，失败转为求助引导）；技术方案 第4章 4.4 模型分层策略（处方用小模型，任务本质是改写）
- 技术方案 第11章 回访（`build_followup_plan` 与药害分支的 T+0 回访）；技术方案 第8章 8.3.3 数字溯源扫描（处方文本中的数字必须全部登记来源）
- 技术方案 第9章 9.5 油菜流程的红线处理（L3 处置知识检索开关设为 disabled）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/prescriber.py` | `PrescriberAgent`、药剂候选检索、溯源登记 |
| 新增 | `app/agent/agents/prompts/prescriber.py` | 话术改写提示词 |
| 新增 | `app/agent/agents/followup_builder.py` | `build_followup_plan` 与药害分支 |
| 新增 | `tests/agent/test_prescriber_agent.py` | 来源约束、溯源登记、空候选的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `PrescriberAgent` |

## 实现要点

### 类骨架

```python
class PrescriberAgent(BaseAgent[Prescription]):
    name = "prescriber"
    output_schema = Prescription
    task_type = TaskType.PRESCRIBE
    failure_mode = FailureMode.DEGRADE          # 转为求助引导
    max_attempts = 1                            # 改写任务，重试收益低
    def __init__(self, gateway, knowledge: KnowledgeRetriever, region_matcher: RegionMatcher) -> None:
        super().__init__(gateway)
        self._knowledge = knowledge
        self._region = region_matcher
```

`max_attempts = 1` 与其他主链路智能体不同。处方的模型调用只做话术改写，事实部分已在代码里定好，一次改写失败（如 JSON 格式错）重试收益很低，直接降级为求助引导更快。

### 确定性检索药剂候选

```python
    async def fetch_pesticide_candidates(self, state, ctx) -> list[PesticideItem]:
        """确定性检索：依据流程、判定结论、地区、用药史取候选条目。"""
        config = ctx.process_config
        diagnosis = DiagnosisResult.model_validate(state["diagnosis"])
        if config.process_id is ProcessId.RAPESEED:
            return []                              # L3 检索开关 disabled
        query = EvidenceQuery(
            process_id=config.process_id,
            keywords=[diagnosis.conclusion, *diagnosis.basis[:2]],
            region_code=ctx.region_code,
            slot_snapshot=slot_summary(state),
            kind_filter=["pesticide"],
            top_k=6,
        )
        chain = self._region.build_chain(ctx.region_code)
        items: list[EvidenceItem] = []
        for _, code in chain:
            items = await self._knowledge.search(query.model_copy(update={"region_code": code}),
                                                 region_chain=[c for _, c in chain])
            if items:
                break
        return [self._to_pesticide_item(i) for i in items if self._passes_history(i, state)]
    def _to_pesticide_item(self, item: EvidenceItem) -> PesticideItem:
        """知识条目到药剂条目的映射。数值全部照抄，不改写。"""
        return PesticideItem(
            knowledge_id=item.knowledge_id, name=item.title,
            dosage_min=item.dosage_min, dosage_max=item.dosage_max, dosage_unit=item.dosage_unit,
            safety_interval_days=item.safety_interval_days, max_uses_per_season=item.max_uses_per_season,
            registered_crops=list(item.registered_crops),
            forbidden_combinations=list(item.forbidden_combinations),
            source_note=f"knowledge:{item.knowledge_id}",
        )
```

油菜流程直接返回空列表，实现技术方案 9.5 的"把 L3 处置知识的检索开关设为 disabled"。检索层不放行比红线拦截更早生效，红线作为第二道防线。`_passes_history` 依据档案用药史剔除重复用药与禁忌搭配，这是 `R-CONFLICT` 的输入侧预过滤，闸门仍会二次校验。 转换是**逐字段照抄**，没有计算或推断，这保证"模板出事实"。

### 溯源登记

```python
    def build_provenance(self, items: list[PesticideItem]) -> dict[str, str]:
        """为处方中每个数值登记来源。键为字段路径。"""
        prov: dict[str, str] = {}
        for i, item in enumerate(items):
            base = f"knowledge:{item.knowledge_id}"
            for field in ("dosage_min", "dosage_max", "safety_interval_days", "max_uses_per_season"):
                if getattr(item, field) is not None:
                    prov[f"pesticide_items[{i}].{field}"] = f"{base}:field:{field}"
        return prov
    def conservative_top_level(self, items: list[PesticideItem]) -> tuple[int | None, int | None]:
        """顶层安全间隔期与每季次数取候选中最保守者（间隔最长、次数最少）。"""
        intervals = [i.safety_interval_days for i in items if i.safety_interval_days is not None]
        uses = [i.max_uses_per_season for i in items if i.max_uses_per_season is not None]
        return (max(intervals) if intervals else None, min(uses) if uses else None)
```

来源标识用 `knowledge:<id>:field:<name>` 的精确到字段形式（技术方案 5.3.5 的示例结构），让闸门的反向扫描能回答"这个数字具体对应条目里的哪个字段"。顶层字段取最保守值：候选有多个时展示最保守的一个不会误导农户把间隔期报短。

### 模型只做改写与强制覆写

```python
    def build_messages(self, state, ctx) -> list[dict]:
        config = ctx.process_config
        candidates = state.get("_pesticide_candidates") or []
        system = [
            f"你是{config.role_definition}，负责把已确定的事实组织成农户能看懂的处方。",
            f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(Prescription.model_json_schema(), ensure_ascii=False)}",
            "硬规则：不得新增、修改或删除任何药剂条目。",
            "硬规则：不得改动任何数字。数字只能原样使用下面给出的值。",
            f"输出段落顺序：{'、'.join(config.output_template.section_order)}。",
            f"总字数不超过 {config.output_template.max_chars} 字。",
            f"必含内容：{'、'.join(config.output_template.required_sections) or '无'}。",
            f"面向农户的表述：{format_colloquial(config.terminology.colloquial_map)}",
        ]
        user = [f"判定结论：{state['diagnosis']}",
                f"可用药剂候选（不可增删）：{format_candidates(candidates)}",
                f"已确认信息：{format_slots(state['slots'])}"]
        return [{"role": "system", "content": "\n".join(system)},
                {"role": "user", "content": "\n".join(user)}]
    async def post_validate(self, result: Prescription, state, ctx) -> Prescription:
        """用代码的结果强制覆写模型可能篡改的部分。"""
        candidates: list[PesticideItem] = state.get("_pesticide_candidates") or []
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
```

三道覆写各有针对性：`pesticide_items` 被强制换成候选集的子集，因此剂量与间隔期不可能是模型编的（`keep_ids` 为空时退回全部候选，避免模型错误 id 导致处方无药剂）；顶层数值取最保守值，不接受模型给的值；`facts_provenance` 完全由代码重建，不读模型输出，使溯源与实际数值必然一致。`requires_tech_station` 在候选为空时强制为 True，符合技术方案 5.3.5。

### 改动检测

```python
    def detect_tampering(self, result: Prescription, candidates: list[PesticideItem]) -> list[str]:
        """检测模型是否试图改动事实。覆写已保证不生效，此处仅为可观测。"""
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
```

覆写已保证模型的改动不会生效，但仍要检测并记录。这条指标（模型试图改动事实的比例）是判断提示词质量与是否需要换模型的直接依据，也是技术方案 15 风险表里"模型改写事实性内容"的监控手段。

### 回访计划与降级

```python
def build_followup_plan(process_id: ProcessId, branch: str | None) -> FollowupPlan:
    """回访计划：按流程取值，药害分支立即触发。"""
    if branch == "herbicide_damage":
        return FollowupPlan(tasks=[FollowupTask(
            due_offset_days=0, kind="preserve_scene",
            content_rule="weed.damage_evidence_checklist",
            ask="现在请先保护现场并拍照记录，不要清理")])
    policy = load_process_config(process_id).followup_policy
    return FollowupPlan(tasks=[
        FollowupTask(due_offset_days=t.after_days, kind=t.kind, ask=t.ask,
                     window_based=policy.window_based)
        for t in policy.triggers])
    async def degrade(self, state, ctx, reason: str) -> Prescription:
        """降级：转为求助引导，不输出任何药剂。"""
        return Prescription(
            diagnosis_summary="当前无法给出完整处置建议，建议咨询当地农技站。",
            actions=[PrescriptionAction(
                order=1, action="到当地农技站咨询", timing="尽快",
                method="携带症状照片与本次问诊记录",
                cautions=["不要自行加大用药量"],
                stop_conditions=["症状明显加重时立即联系农技人员"])],
            safety_interval_days=None, max_uses_per_season=None,
            pesticide_items=[], soil_level_actions=[],
            followup_plan=build_followup_plan(ctx.process_config.process_id, None),
            facts_provenance={}, requires_tech_station=True, detail_terms={})
```

药害分支的 `due_offset_days=0` 与 `kind="preserve_scene"` 是杂草流程的特殊回访：药害鉴定要求保留典型性状，错过表现期将无法鉴别起因，因此首次回访必须是"立即"且内容是现场固定动作清单而非效果询问。降级的 `actions` 只有一条"到农技站咨询"，不含任何具体药剂与剂量，与安全闸门 `degrade` 后的输出形态一致。降级处方必须能通过闸门校验，否则会出现"降级后又降级"的链路；`facts_provenance={}` 之所以能通过，是因为没有数值字段，`numeric_paths()` 返回空列表。

## 验收标准

- `pytest tests/agent/test_prescriber_agent.py` 全部通过，含三类用例：模型返回候选之外的 `knowledge_id` 时被丢弃并退回候选集、模型篡改 `dosage_max` 时实际输出仍为原值、候选为空时 `requires_tech_station=True` 且 `pesticide_items=[]`；断言油菜流程的 `fetch_pesticide_candidates` 返回空列表且不调用检索
- 断言 `facts_provenance` 的键集合等于 `Prescription.numeric_paths()` 的结果；断言 `conservative_top_level` 在候选间隔期为 `[7, 10]` 时返回 `10`
- 断言 `detect_tampering` 能识别出新增条目与改动数值两类情况；断言 `build_followup_plan` 在 `branch="herbicide_damage"` 时返回单任务且 `due_offset_days=0`、`kind="preserve_scene"`
- 断言 `build_followup_plan` 对辣椒流程返回两个任务，`due_offset_days` 分别为 3 与 7；断言降级返回的处方可被 `Prescription.model_validate` 通过，且 `numeric_paths()` 为空列表
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
