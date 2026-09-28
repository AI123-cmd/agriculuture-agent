"""闸门契约：全部输入类型与裁定输出（T2-34，技术方案 5.3.6 与 8.1）。

安全闸门是本方案**唯一拥有否决权**的组件，它的失败行为是 fail-closed：任何一处判断不出
结论，安全结论都是"不放行"，而不是按其余智能体的模式降级继续服务。本模块把这个差别写成
可构造性约束——`process_redlines_loaded=False` 时构造不出 `decision="allow"` 的结果。

**契约本体归闸门所有**：这里是 `GateResult`/`GateViolation`/`GateContext` 的唯一定义处，
agent 侧（`app/agent/contracts/gate.py`）改为 `from app.gate.contracts import ...` 再导出。
L1 只禁止 gate → agent，agent → gate 方向合法。

`GateContext` 不含会话消息（`messages`/`conversation`/`history`）是**物理隔离**的实现：
闸门拿不到会话文本，因此无法被对话中的表述影响（技术方案 8.1）。`extra="forbid"` 会让
"顺手塞一个 messages 进去"在构造时报错，把文档约定变成类型约束。

`GateContext` 用 Pydantic 而非 `dict`；`knowledge` 声明为 `Protocol`（见 `ports.py`）而非具体
仓储类型，是 L1 与可测性的共同要求。处方输入是 `GatePrescription`——与 T2-11 的 `Prescription`
**同名同义的只读镜像**，理由同样是闸门不得导入生成链路。
"""

from collections.abc import Mapping
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.process_config.redlines import LoadedRedlines

from .enums import GateSeverity, ProcessId
from .ports import KnowledgeFacts

# decision 语义：
# allow   —— 放行，处方卡正常返回
# degrade —— 不通过但不属严重违规，输出整体替换为 degrade_output
# block   —— 命中硬红线，拒绝输出并记录审计事件，同时触发知识条目复核工单

# 需要溯源到字段级的药剂数值字段。与 T2-11 `Prescription` 的同名常量保持同一份清单，
# 供 `R-PROVENANCE` 的反向扫描与 `R-NO-DATA` 的正向核对共用，避免两处清单漂移。
_PESTICIDE_NUMERIC_FIELDS: tuple[str, ...] = (
    "dosage_min",
    "dosage_max",
    "safety_interval_days",
    "max_uses_per_season",
)


class GateViolation(BaseModel):
    """一条违规明细。审计表里的 `violations` 是这一对象的 JSON 数组。

    `rule_id` 用字符串而非枚举：契约要能被审计表原样存储，且未来可能引入带参数的规则
    实例（如 `R-DOSAGE:unit=ml`）。枚举校验留在规则引擎侧，不在契约侧。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    severity: GateSeverity
    message: str
    field: str  # 违规字段路径，如 pesticide_items[K-1023]、slots.next_pick_date


class GateResult(BaseModel):
    """门禁输出。技术方案 5.3.6 原样定义。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    violations: list[GateViolation]
    checked_rules: list[str]  # 实际执行的规则 id，供审计反查漏配
    process_redlines_loaded: bool
    decision: Literal["allow", "degrade", "block"]
    degrade_output: str | None = None
    # 影子模式（T2-46）：外层是 `passed=True` 的"放行外壳"，内层才是真实判定。
    # 生产环境不允许 `GATE_MODE=shadow`，此字段只在灰度期非空。
    shadowed: "GateResult | None" = None

    @model_validator(mode="after")
    def _redlines_missing_forces_block(self) -> "GateResult":
        """红线缺失即不可用。

        产品说明书 9.3 明确"流程专属红线不可缺省：某条流程的红线配置缺失时，该流程应处于
        不可用状态，禁止以通用规则代替"。把这条从"实现者要记得判断"变成"构造不出违反它的
        对象"，是 fail-closed 在契约层的执行点。
        """
        if not self.process_redlines_loaded and self.decision != "block":
            raise ValueError("流程红线未加载时必须判定 block")
        return self

    @model_validator(mode="after")
    def _decision_consistency(self) -> "GateResult":
        """拦"有 block 级违规但仍放行"与"放行却不通过"两类显然错误。

        影子模式例外：外层是"放行"外壳（`passed=True`），内层 `shadowed` 才是真实判定，
        两者必然看起来矛盾，因此显式跳过（T2-46）。
        """
        if self.shadowed is not None:
            return self
        has_block = any(v.severity == "block" for v in self.violations)
        if has_block and self.decision == "allow":
            raise ValueError("存在 block 级违规时不得 allow")
        if self.decision == "allow" and not self.passed:
            raise ValueError("decision=allow 时 passed 必须为 True")
        return self

    @model_validator(mode="after")
    def _degrade_needs_output(self) -> "GateResult":
        """degrade 的输出整体替换为 `degrade_output`，没有文案的 degrade 等于没有输出。

        产品说明书 6.2 要求"校验不通过时，输出降级为建议联系当地农技站，而不是修改后输出"，
        因此这一档必须自带一份完整文案，而不是由调用方自己拼。
        """
        if self.decision == "degrade" and not self.degrade_output:
            raise ValueError("degrade 必须给出降级文案")
        return self


class GatePesticideItem(BaseModel):
    """`PesticideItem`（T2-11）的只读镜像，字段同名同义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

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


class GateAction(BaseModel):
    """`PrescriptionAction`（T2-11）的只读镜像。`R-REDLINE` 扫动作文本时读这些字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int
    action: str
    timing: str
    method: str
    cautions: list[str] = []
    stop_conditions: list[str] = []


class GatePrescription(BaseModel):
    """`Prescription`（T2-11）的只读镜像。字段与 T2-11 同名，闸门不导入生成链路。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnosis_summary: str
    actions: list[GateAction] = []
    safety_interval_days: int | None = None
    max_uses_per_season: int | None = None
    pesticide_items: list[GatePesticideItem] = []
    soil_level_actions: list[str] = []
    followup_plan: dict[str, object] = {}
    facts_provenance: dict[str, str] = {}
    requires_tech_station: bool = False
    detail_terms: dict[str, str] = {}

    @classmethod
    def from_contract(cls, contract: object) -> "GatePrescription":
        """把 T2-11 的 `Prescription`（或等价 dict）转成镜像。

        允许传入对象或 dict 两种形态：闸门不得导入 `app.agent`，因此这里只做鸭子类型转换，
        不写 `from app.agent.contracts import Prescription`。
        """
        if isinstance(contract, cls):
            return contract
        if isinstance(contract, Mapping):
            return cls.model_validate(dict(contract))
        dump = getattr(contract, "model_dump", None)
        if callable(dump):
            return cls.model_validate(dump(mode="json"))
        raise TypeError(f"无法转换为 GatePrescription：{type(contract).__name__}")

    def numeric_paths(self) -> list[str]:
        """列出本处方中所有需要溯源的数值字段路径（与 T2-11 同名方法一致）。

        无药剂条目时枚举为空，因此"无药剂就不会出现药剂数值路径"由结构本身保证。
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


class MedicationRecord(BaseModel):
    """档案中的一条用药记录。`R-MAX-USE` 读它做次数累计，`R-CONFLICT` 读它判重复与禁忌。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_id: str | None = None
    pesticide_name: str
    used_on: date


class GatePayload(BaseModel):
    """处方卡最终展示文本，是 `R-PROVENANCE`/`R-REDLINE`/`R-CAUTION` 的反向扫描输入。

    取值顺序为：`detail_terms` 替换后的动作文本 → 拼接 `cautions` → 拼接 `stop_conditions`。
    三条规则读同一个字段，避免"扫描 A 文本、放行 B 文本"的漏洞。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rendered_text: str = ""


class GateContext(BaseModel):
    """闸门全部输入。刻意不含会话消息，也不含任何模型句柄。"""

    # `arbitrary_types_allowed` 是 `knowledge: Protocol` 所必需的：协议不是可校验的类型，
    # 闸门对它的要求只是"能调用声明的方法"，由调用方保证传入的实现可用。
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    process_id: ProcessId
    config_version: int
    prescription: GatePrescription
    payload: GatePayload = GatePayload()
    redlines: LoadedRedlines
    slots: dict[str, str | date | None] = {}
    knowledge: KnowledgeFacts
    medication_history: list[MedicationRecord] = []
    region_code: str | None = None
    today: date
    season_start: date | None = None  # 每季次数的统计起点
    rule_configs: dict[str, dict[str, object]] = {}  # rule_id -> 红线 data 载荷
    # 数值 token（如 "30克"）→ 字段路径的映射，由处方层在 build_provenance 时同步填充。
    # `facts_provenance` 的键是**字段路径**（pesticide_items[0].dosage_max），而文本里出现的
    # 是**数值加单位**（30克），两者形态不同，直接比对必然全部不匹配；因此生成侧额外提供
    # 这份映射供 `R-PROVENANCE` 做反向扫描（T2-41 的必要推断）。
    numeric_tokens: dict[str, str] = {}

    def slot_date(self, key: str) -> date | None:
        """取日期型槽位。空、unknown、解析失败一律返回 None（fail-closed）。

        返回 None 使调用方（如 `R-INTERVAL` 的茶流程分支）走"缺少采摘计划"的保守分支——
        解析失败也不能证明方案不冲突，因此与"未填"同样处理。
        """
        raw = self.slots.get(key)
        if raw in (None, "", "unknown"):
            return None
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return None

    def registered_numeric_tokens(self) -> dict[str, str]:
        """返回「数值 token → 字段路径」。由处方层在构建溯源表时同步填充。

        为什么不是直接用 `facts_provenance` 的键：键是字段路径，文本里出现的是数值加单位，
        形态不同，必须由生成侧提供映射才能做反向扫描。
        """
        return dict(self.numeric_tokens)


__all__ = [
    "GateAction",
    "GateContext",
    "GatePayload",
    "GatePesticideItem",
    "GatePrescription",
    "GateResult",
    "GateViolation",
    "KnowledgeFacts",
    "MedicationRecord",
]
