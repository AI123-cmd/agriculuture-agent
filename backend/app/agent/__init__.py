"""LangGraph 编排层入口：暴露枚举、状态定义与 build_graph / get_agent。"""

from langgraph.graph.state import CompiledStateGraph

from .graph.main import build_graph
from .state import (
    SOURCE_PRIORITY,
    AnswerType,
    ConfidenceLevel,
    ConsultationState,
    ConsultStage,
    EvidenceItem,
    EvidenceKind,
    GateDecision,
    ObjectType,
    PanelResolution,
    ProcessId,
    SlotGroup,
    SlotOwner,
    SlotSource,
    SlotStatus,
    SlotValue,
    TaskType,
    TriggerSource,
    ViolationSeverity,
    parse_process_id,
    parse_stage,
)

__all__ = [
    "SOURCE_PRIORITY",
    "AnswerType",
    "ConfidenceLevel",
    "ConsultStage",
    "ConsultationState",
    "EvidenceItem",
    "EvidenceKind",
    "GateDecision",
    "ObjectType",
    "PanelResolution",
    "ProcessId",
    "SlotGroup",
    "SlotOwner",
    "SlotSource",
    "SlotStatus",
    "SlotValue",
    "TaskType",
    "TriggerSource",
    "ViolationSeverity",
    "build_graph",
    "get_agent",
    "parse_process_id",
    "parse_stage",
]

_agent: CompiledStateGraph | None = None


async def get_agent() -> CompiledStateGraph:
    """返回编译后的诊断图单例（含 PostgreSQL checkpointer）。

    Postgres checkpointer 用连接池，不绑事件循环，跨循环复用同一个 saver 是安全的，
    故不再保留 `_agent_loop` 判断——那会在测试隔离时误触发重建（T2-30）。
    """
    global _agent
    if _agent is None:
        _agent = await build_graph()
    return _agent
