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
from app.agent.agents.retriever import KnowledgeRetriever, RegionMatcher, RetrieverAgent
from app.agent.agents.triage import TriageAgent, run_triage

__all__ = [
    "AgentContext",
    "AgentDegraded",
    "AgentFailure",
    "BaseAgent",
    "CandidatePool",
    "CandidateSlot",
    "ContractViolation",
    "DiagnoserAgent",
    "FailureMode",
    "FieldObserverAgent",
    "InterviewerAgent",
    "KnowledgeRetriever",
    "PERSPECTIVE_FOCUS",
    "PanelAggregate",
    "PanelOpinion",
    "PanelRunner",
    "RawReading",
    "RegionMatcher",
    "RetrieverAgent",
    "TriageAgent",
    "aggregate_panel",
    "all_candidates",
    "classify_reading",
    "reduce_exclusions",
    "run_triage",
    "select_candidate_slots",
]
