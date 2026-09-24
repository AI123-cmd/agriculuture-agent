"""智能体包（T2-13 起）。

每个智能体声明四件事：输出契约类型（`output_schema`）、提示词构造（`build_messages`）、
降级实现（`degrade`）、失败行为类别（`failure_mode`）。基类负责契约校验、有限次重试
与降级分派，具体智能体不重复写这些横切逻辑。
"""

from app.agent.agents.base import AgentContext, AgentFailure, BaseAgent, FailureMode
from app.agent.agents.candidate_pool import CandidatePool, CandidateSlot, select_candidate_slots
from app.agent.agents.diagnoser import DiagnoserAgent
from app.agent.agents.exceptions import AgentDegraded, ContractViolation
from app.agent.agents.field_observer import FieldObserverAgent, RawReading, classify_reading
from app.agent.agents.followup_builder import build_followup_plan
from app.agent.agents.handoff import (
    HandoffBuilder,
    HandoffObservation,
    HandoffPackage,
    HandoffReason,
    SummaryLinkBuilder,
    build_handoff_node,
)
from app.agent.agents.interviewer import InterviewerAgent
from app.agent.agents.panel import (
    PERSPECTIVE_FOCUS,
    PanelAggregate,
    PanelOpinion,
    PanelRunner,
    aggregate_panel,
    all_candidates,
    reduce_exclusions,
)
from app.agent.agents.prescriber import PrescriberAgent
from app.agent.agents.report_guard import ContactDirectory, ReportGuardAgent, build_report_guard_node
from app.agent.agents.report_signals import PIG_SIGNALS, SIGNAL_RULES, SignalRule, match_signals, resolve_trigger_source
from app.agent.agents.retriever import KnowledgeRetriever, RegionMatcher, RetrieverAgent
from app.agent.agents.triage import TriageAgent, run_triage

__all__ = [
    "AgentContext",
    "AgentDegraded",
    "AgentFailure",
    "BaseAgent",
    "CandidatePool",
    "CandidateSlot",
    "ContactDirectory",
    "ContractViolation",
    "DiagnoserAgent",
    "FailureMode",
    "FieldObserverAgent",
    "HandoffBuilder",
    "HandoffObservation",
    "HandoffPackage",
    "HandoffReason",
    "InterviewerAgent",
    "KnowledgeRetriever",
    "PERSPECTIVE_FOCUS",
    "PIG_SIGNALS",
    "PanelAggregate",
    "PanelOpinion",
    "PanelRunner",
    "PrescriberAgent",
    "RawReading",
    "RegionMatcher",
    "ReportGuardAgent",
    "RetrieverAgent",
    "SIGNAL_RULES",
    "SignalRule",
    "SummaryLinkBuilder",
    "TriageAgent",
    "aggregate_panel",
    "all_candidates",
    "build_followup_plan",
    "build_handoff_node",
    "build_report_guard_node",
    "classify_reading",
    "match_signals",
    "reduce_exclusions",
    "resolve_trigger_source",
    "run_triage",
    "select_candidate_slots",
]
