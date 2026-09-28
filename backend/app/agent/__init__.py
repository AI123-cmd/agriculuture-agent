"""LangGraph 编排层入口：暴露枚举、状态定义与 build_graph / get_agent / resume_graph。"""

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

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
    "resume_graph",
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


async def resume_graph(session_id: str, resume_value: object) -> dict:
    """按 `thread_id` 恢复中断的图，传入农户的回答或确认选择（T2-32）。

    `session_id` 即检查点的 `thread_id`（T2-30）：服务重启后第一个带 `sessionId` 的请求
    用它取回同一线程的状态，从断点继续，而不是当成新会话从头再问一遍。
    `resume_value` 的形态由中断点决定——问诊类中断收答案，闸门确认类中断收
    `"continue"` / `"abort"`。
    """
    graph = await get_agent()
    config = {"configurable": {"thread_id": session_id}}
    return await graph.ainvoke(Command(resume=resume_value), config=config)
