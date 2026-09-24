import asyncio

from app.agent.contracts.diagnosis import DiagnosisResult
from app.agent.contracts.evidence import EvidenceSet
from app.agent.contracts.interview import InterviewBatch
from app.agent.contracts.observation import ObservationResult
from app.agent.contracts.prescription import Prescription
from app.agent.contracts.report import ReportDecision
from app.agent.contracts.triage import TriageResult
from app.llm.providers.mock import MockProvider

SCHEMAS = [
    TriageResult,
    ObservationResult,
    InterviewBatch,
    EvidenceSet,
    DiagnosisResult,
    Prescription,
    ReportDecision,
]


async def main() -> None:
    provider = MockProvider()
    for schema in SCHEMAS:
        resp = await provider.chat("m", [], schema)
        print(f"{schema.__name__}: parsed={resp.parsed is not None} error={resp.error}")


asyncio.run(main())
