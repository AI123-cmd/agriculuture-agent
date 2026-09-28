"""安全闸门包（T2-34）。本方案**唯一拥有否决权**的组件。

三条与生成链路物理隔离的设计（技术方案 8.1）：
1. 不接收会话消息——`GateContext` 用 `extra="forbid"` 挡住 `messages`/`conversation`/`history`；
2. 不调用大模型——全部规则是纯函数，每次判定可复现；
3. 不依赖智能体层——L1 禁止 `app.gate` 导入 `app.agent`/`app.services`，契约本体归本包所有。

失败行为是 fail-closed（全局约定第六节）：任何一处判断不出结论，安全结论都是"不放行"。
"""

from app.gate.contracts import (
    GateAction,
    GateContext,
    GatePayload,
    GatePesticideItem,
    GatePrescription,
    GateResult,
    GateViolation,
    MedicationRecord,
)
from app.gate.engine import GateEngine
from app.gate.enums import GateDecision, GateRuleId, GateSeverity, ProcessId, ViolationSeverity
from app.gate.ports import KnowledgeEntrySnapshot, KnowledgeFacts
from app.gate.rules.base import GateConfigError, Rule, build_rules, register

__all__ = [
    "GateAction",
    "GateConfigError",
    "GateContext",
    "GateDecision",
    "GateEngine",
    "GatePayload",
    "GatePesticideItem",
    "GatePrescription",
    "GateResult",
    "GateRuleId",
    "GateSeverity",
    "GateViolation",
    "KnowledgeEntrySnapshot",
    "KnowledgeFacts",
    "MedicationRecord",
    "ProcessId",
    "Rule",
    "ViolationSeverity",
    "build_rules",
    "register",
]
