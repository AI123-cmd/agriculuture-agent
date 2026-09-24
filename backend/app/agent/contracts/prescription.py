"""处方契约（技术方案 5.3.5）。

本模块把"模板出事实、模型只改写"从原则变成自动检查：`facts_provenance` 是全处方数值字段的
登记表，`_provenance_covers_numbers` 在对象构造完成时做正向核对——`numeric_paths()` 列出的
每个路径都必须在表中出现，缺一个就抛错。模型若漏登记来源，处方在解析阶段即被拦住，而不是
等闸门（T2-41）做反向扫描时才发现。

`pesticide_items` 的来源约束是另一条底线：技术方案 5.3.5 要求药剂条目只能由代码从知识库
检索结果填充，模型无权写入。契约层用一个必填的 `knowledge_id` 把这件事固定下来——闸门
`R-WHITELIST` 要求每一项的 `knowledge_id` 存在于已核准知识库，没有它就无从校验，因此它既
不能为空，也不能是模型编造的值。

与闸门的分工：契约只保证**结构完整**，语义违规（如涉及用药却没给安全间隔期）交给闸门
`R-INTERVAL` 拦截并降级为转人工引导。若契约层直接抛错，处方节点会重试并可能反复失败，而
此时正确的行为是让闸门给出 `degrade` 输出——这与"闸门是唯一拥有否决权的组件"一致。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

# 需要溯源到字段级的药剂数值字段。写成模块级常量而不是在 `numeric_paths` 里就地写字面量，
# 是为了让 T2-41 的反向扫描与本方法用同一份清单，避免两处清单漂移后出现"一边认它有数值、
# 另一边不认"的漏检。
_PESTICIDE_NUMERIC_FIELDS: tuple[str, ...] = (
    "dosage_min",
    "dosage_max",
    "safety_interval_days",
    "max_uses_per_season",
)


class PesticideItem(BaseModel):
    """药剂条目。只能由代码从知识库检索结果填充，模型无权写入。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str
    name: str
    dosage_min: float | None = None
    dosage_max: float | None = None
    dosage_unit: str | None = None
    safety_interval_days: int | None = None
    max_uses_per_season: int | None = None
    registered_crops: list[str] = []
    forbidden_combinations: list[str] = []
    source_note: str | None = None


class PrescriptionAction(BaseModel):
    """一条处置动作。

    `stop_conditions` 强制非空。技术方案与产品说明书反复强调"把握不足时给出观察 + 复查"，
    而行动建议若无停止条件，农户会一路执行到出现明显损失。"何时该停"因此必须是每条动作的
    固有部分，而不是可选的补充说明。
    """

    model_config = ConfigDict(extra="forbid")

    order: int
    action: str
    timing: str
    method: str
    cautions: list[str]
    stop_conditions: list[str]

    @model_validator(mode="after")
    def _stop_conditions_present(self) -> "PrescriptionAction":
        if not self.stop_conditions:
            raise ValueError("每条动作必须给出停止或求助条件")
        return self


class FollowupTask(BaseModel):
    """一条回访任务。

    `due_offset_days` 是相对当天的天数（0 表示立即），而不是绝对日期：处方可能在任意时刻
    生成，而回访时点总是相对于动作。杂草流程的药害分支用 `due_offset_days=0` 配
    `kind="preserve_scene"` 表达"立即固定现场"，`content_rule` 指向
    `weed.damage_evidence_checklist` 供文案层取模板。
    """

    model_config = ConfigDict(extra="forbid")

    due_offset_days: int
    kind: Literal["effect_check", "risk_check", "preserve_scene", "observe"]
    content_rule: str | None = None
    ask: str | None = None
    window_based: bool = False


class FollowupPlan(BaseModel):
    """回访计划。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[FollowupTask]
    note: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "FollowupPlan":
        """任务按时间升序，在契约层一次排定。

        回访服务的调度因此不必自己排序，也不必担心处方层给出乱序导致同一天多次触达农户。
        """
        self.tasks.sort(key=lambda t: t.due_offset_days)
        return self


class Prescription(BaseModel):
    """处方契约。技术方案 5.3.5 原样定义。"""

    model_config = ConfigDict(extra="forbid")

    diagnosis_summary: str
    actions: list[PrescriptionAction]
    safety_interval_days: int | None
    max_uses_per_season: int | None
    pesticide_items: list[PesticideItem]
    soil_level_actions: list[str]
    followup_plan: FollowupPlan
    facts_provenance: dict[str, str]
    requires_tech_station: bool
    detail_terms: dict[str, str]

    @model_validator(mode="after")
    def _provenance_covers_numbers(self) -> "Prescription":
        """事实必带溯源的契约层执行点：数值路径未登记来源即抛错。

        正向核对（本方法）与闸门的反向扫描（T2-41）方向相反、互为补充：前者保证模型产出的
        数字都有出处，后者保证引用的出处确实存在且可用。
        """
        missing = [k for k in self.numeric_paths() if k not in self.facts_provenance]
        if missing:
            raise ValueError(f"数值字段缺少溯源: {missing}")
        return self

    @model_validator(mode="after")
    def _pesticide_items_need_provenance(self) -> "Prescription":
        """这条刻意不阻断：涉及用药但未给出安全间隔期。

        技术方案 8.2 把这条归为闸门规则 `R-INTERVAL`（违规等级 `block`），处置是拦截并降级为
        转人工引导。若契约层抛错，处方节点会重试、可能反复失败，而正确行为是让闸门给出
        `degrade` 输出。契约在此只标记判定点，把否决权留给闸门。
        """
        return self

    def numeric_paths(self) -> list[str]:
        """列出本处方中所有需要溯源的数值字段路径。

        无药剂条目时枚举为空，因此"无药剂就不会出现药剂数值路径"由结构本身保证，无需额外
        分支判断。
        """
        paths: list[str] = []
        if self.safety_interval_days is not None:
            paths.append("safety_interval_days")
        if self.max_uses_per_season is not None:
            paths.append("max_uses_per_season")
        for i, item in enumerate(self.pesticide_items):
            for field in _PESTICIDE_NUMERIC_FIELDS:
                if getattr(item, field) is not None:
                    paths.append(f"pesticide_items[{i}].{field}")
        return paths


__all__ = [
    "FollowupPlan",
    "FollowupTask",
    "PesticideItem",
    "Prescription",
    "PrescriptionAction",
]
