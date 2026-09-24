"""流程配置的 Pydantic v2 模型族。

流程配置是全项目的事实来源，它同时被六处消费：分诊读取 `object_aliases`，问诊读取
`required_slots` 与 `terminology`，判定读取 `confusable_items`，处方读取
`output_template`，闸门读取 `redlines`，提示读取 `timing_rules` 与 `calendar_source`。
先用模型把这些字段固化，七份 YAML 才有统一的校验标准，任何一处写错都会在加载阶段
而不是运行阶段暴露。

`extra="forbid"` 是关键：YAML 里写错字段名必须立即报错，而不是被安静忽略——历史上
这类"配置写了但没生效"的问题最难排查，因为它既不报错也不生效。`frozen=True` 使配置
对象不可变，避免某个智能体在处理中途改到配置，从而让"同一次问诊内配置一致"成为
结构性保证。
"""

from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.state import AnswerType, ProcessId, SlotGroup, ViolationSeverity


class SlotSpec(BaseModel):
    """单个信息槽位的规格。

    `label` 是内部用语（"叶发病率"），`display_label` 是农户看的口语化标签
    （"多少叶子有问题"）。`owner` 声明唯一有权写入该槽位的智能体，对应黑板
    「单一写入者」规则（技术方案 6.3）。`source_hint` 列出可自动填充的来源
    （如 `["profile"]` 表示档案里已有则不必问），追问候选池筛选器据此剔除候选。
    `ask_order` 越小越先问，同一批次内的问题按此排序。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    display_label: str
    group: SlotGroup
    required: bool = True
    owner: str
    source_hint: list[str] = []
    options: list[str] | None = None
    ask_order: int = 0


class RedlineSpec(BaseModel):
    """单条流程红线的引用外壳。

    `id` 形如 `TEA-01`；`rule` 可为逗号分隔的多规则引用。完整红线语义（`data`
    载荷结构与完整性校验）在 `redlines.py` 展开，本类只负责把 YAML 形态固化。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    desc: str
    rule: str
    severity: ViolationSeverity
    data: dict = {}


class TerminologySpec(BaseModel):
    """术语表。两套术语必须分开：`internal_terms` 给模型与检索用，
    `colloquial_map` 给展示层做替换。展示层渲染时必须过 `colloquial_map`。

    `display_metrics` 是本流程优先使用的指标口径（茶用"叶发病率"、水稻用
    "病丛率/百丛虫量"、杂草用"经济阈值"）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    internal_terms: dict[str, str]
    colloquial_map: dict[str, str]
    display_metrics: list[str]


class TimingRule(BaseModel):
    """时机判据，供提示快照生成使用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    when: dict
    prompt: str


class OutputTemplateSpec(BaseModel):
    """输出模板：模板出事实、模型只改写。

    `max_chars` 默认 400，配合适老化改造；上限由后端负责，不留给前端截断，
    否则适老化验收时会反复返工。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_order: list[str]
    max_chars: int = 400
    required_sections: list[str] = []


class FollowupTaskSpec(BaseModel):
    """单条回访触发规则。`after_days` 是相对天数。

    药害分支的 T+0 回访不走这里：它由 `branch == "herbicide_damage"` 直接构造
    （T2-21），因为那是"立刻要做的现场固定动作"，不是按天计的回访计划。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    after_action: str | None = None
    after_days: int
    ask: str
    kind: str = "effect_check"


class FollowupPolicy(BaseModel):
    """流程的回访策略。`window_based=True` 表示按生育期窗口而非固定天数回访。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    triggers: list[FollowupTaskSpec]
    window_based: bool = False


class OfflineQuestion(BaseModel):
    """无传感器时的增强追问。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_key: str
    text: str
    options: list[str] | None = None
    answer_type: AnswerType = AnswerType.SINGLE


class ConfusablePair(BaseModel):
    """一组易混淆项及其决定性特征。

    这份配置一份驱动三处：问诊据此生成关键追问，聚合器据此做排除裁定，
    判定智能体据此填充 `excluded` 字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[str]
    decisive_features: dict[str, list[str]]
    must_exclude: bool = True

    @model_validator(mode="after")
    def _features_cover_candidates(self) -> "ConfusablePair":
        """每个候选都必须有决定性特征。

        否则聚合器会在硬判据分支上遇到 `KeyError`——而那时已经进入判定流程，
        报错信息会离配置错误很远，排查成本极高。
        """
        missing = set(self.candidates) - set(self.decisive_features)
        if missing:
            raise ValueError(f"候选 {sorted(missing)} 缺少决定性特征")
        return self


class ProcessConfig(BaseModel):
    """一条流程的完整配置。加载成功即视为该流程可路由。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process_id: ProcessId
    version: int
    display_name: str
    role_definition: str
    required_slots: list[SlotSpec]
    redlines: list[RedlineSpec]
    terminology: TerminologySpec
    timing_rules: list[TimingRule]
    output_template: OutputTemplateSpec
    calendar_source: str | None
    followup_policy: FollowupPolicy
    offline_questions: list[OfflineQuestion]
    panel_enabled: bool = False
    object_aliases: list[str] = []
    confusable_items: list[ConfusablePair]
    banned_topics: list[str] = []

    def required_slot_keys(self) -> list[str]:
        """必问槽位的 key 列表（`required=True` 的那些）。"""
        return [s.key for s in self.required_slots if s.required]

    def slot_spec(self, key: str) -> SlotSpec | None:
        """按 key 查槽位规格；未声明返回 `None`（由调用方决定报错方式）。"""
        return next((s for s in self.required_slots if s.key == key), None)

    def redline_rule_ids(self) -> list[str]:
        """展开逗号分隔的 `rule` 字段，供闸门反查漏配。

        闸门用它做 `checked_rules` 反查，发现"声明了但没执行"的配置错误。
        """
        ids: list[str] = []
        for item in self.redlines:
            ids.extend(r.strip() for r in item.rule.split(",") if r.strip())
        return ids

    def check_owns(self, key: str, owner: str) -> bool:
        """黑板单一写入者规则的判据。写入方不符时由黑板写入函数抛异常。"""
        spec = self.slot_spec(key)
        return spec is not None and spec.owner == owner


__all__ = [
    "ConfusablePair",
    "FollowupPolicy",
    "FollowupTaskSpec",
    "OfflineQuestion",
    "OutputTemplateSpec",
    "ProcessConfig",
    "RedlineSpec",
    "SlotSpec",
    "TerminologySpec",
    "TimingRule",
]
