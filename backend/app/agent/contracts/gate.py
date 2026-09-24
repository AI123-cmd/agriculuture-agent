"""门禁契约（技术方案 5.3.6）。

安全闸门是本方案**唯一拥有否决权**的组件。它的失败行为是 fail-closed：任何一处判断不出
结论，安全结论都是"不放行"，而不是按其余智能体的模式降级继续服务。本模块把这个差别写成
可构造性约束——`process_redlines_loaded=False` 时构造不出 `decision="allow"` 的结果。

`GateContext` 不含会话消息（`messages`/`conversation`/`history`）是**物理隔离**的实现：
闸门拿不到会话文本，因此无法被对话中的表述影响。技术方案 8.1 明确"它不接收会话消息，
因此无法被对话中的表述影响；不调用大模型，因此每次判定都可复现"。把这两条写进字段集合，
比写在文档里更可靠——`extra="forbid"` 会让"顺手塞一个 messages 进去"在构造时报错。

本模块只定义**形态与裁定一致性**，规则清单与规则实现属于 T2-34 及其后各项。
"""

from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.state import ProcessId, SlotValue
from app.process_config.redlines import LoadedRedlines

from .prescription import Prescription

# decision 语义：
# allow   —— 放行，处方卡正常返回
# degrade —— 不通过但不属严重违规，输出整体替换为 degrade_output
# block   —— 命中硬红线，拒绝输出并记录审计事件，同时触发知识条目复核工单


@runtime_checkable
class KnowledgeFacts(Protocol):
    """已核准知识条目的**只读**事实源。

    声明为协议而非具体仓储类型有两个原因。其一是分层：`app/gate/` 不得导入
    `app/services/`，闸门只能声明它需要什么，由调用方传入满足协议的实现。其二是可测性：
    规则单测可以喂三行假实现，不牵动数据库。

    协议里不放 `search`：闸门不做检索——检索属生成侧。闸门只核对已被引用条目的字段，
    这是"独立仲裁"与"生成"在数据上的分界。
    """

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]: ...


class GateViolation(BaseModel):
    """一条违规明细。审计表里的 `violations` 是这一对象的 JSON 数组。

    `rule_id` 用字符串而非 `GateRuleId` 枚举：契约要能被审计表原样存储，且未来可能引入
    带参数的规则实例（如 `R-DOSAGE:unit=ml`）。枚举校验留在规则引擎侧，不在契约侧。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    severity: Literal["block", "warn"]
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
        """拦"有 block 级违规但仍放行"与"放行却不通过"两类显然错误。"""
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


class GateContext(BaseModel):
    """闸门全部输入。刻意不含会话消息，也不含任何模型句柄。"""

    # `arbitrary_types_allowed` 是 `knowledge: Protocol` 所必需的：协议不是可校验的类型，
    # 闸门对它的要求只是"能调用声明的方法"，由调用方保证传入的实现可用。
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    process_id: ProcessId
    config_version: int
    prescription: Prescription
    redlines: LoadedRedlines
    slots: dict[str, SlotValue]
    knowledge: KnowledgeFacts
    medication_history: list[dict]
    region_code: str | None
    today: date
    checked_rule_ids: list[str] = []


__all__ = ["GateContext", "GateResult", "GateViolation", "KnowledgeFacts"]
