"""LangGraph 编排层入口：暴露枚举、状态定义与 build_graph / get_agent。"""

import asyncio

from langgraph.graph.state import CompiledStateGraph

from .graph import build_graph
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
_agent_loop: asyncio.AbstractEventLoop | None = None


async def get_agent() -> CompiledStateGraph:
    """返回编译后的诊断图单例（含 PostgreSQL checkpointer）。

    图绑定创建时的事件循环；跨循环（如测试隔离）自动重建。
    """
    global _agent, _agent_loop
    loop = asyncio.get_running_loop()
    if _agent is None or _agent_loop is not loop:
        _agent = await build_graph()
        _agent_loop = loop
    return _agent
