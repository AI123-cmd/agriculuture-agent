"""智能体契约包（技术方案 5.3）。

契约放的是智能体之间的**数据形态**，不含实现：智能体负责从模型输出里解析出这些
对象，主图负责在节点之间传递它们。用 Pydantic 而不是裸字典的原因与流程配置一致：
契约违约（字段缺失、枚举越界、跨字段矛盾）必须在对象构造时暴露，而不是等到下游
某个 `if` 分支才以 `KeyError` 或静默错误分支的形式出现。

`ConsultationState` 里的产出字段（`evidence`、`diagnosis`、`prescription` 等）都是
`dict` 而非契约对象，因为 TypedDict 交给 LangGraph checkpointer 时需要可 JSON
序列化。因此进入 state 前统一 `dump_contract`，从 state 取出时统一 `load_contract`。
"""

from typing import TypeVar

from pydantic import BaseModel

from .diagnosis import CONFIDENCE_DISPLAY, DiagnosisResult, UncertaintySource
from .evidence import EvidenceItem, EvidenceKindValue, EvidenceQuery, EvidenceSet, RegionLevelActual
from .gate import GateContext, GateResult, GateViolation, KnowledgeFacts
from .interview import (
    SKIP_MARKERS,
    AnswerTypeValue,
    InterviewBatch,
    InterviewQuestion,
    QuestionOption,
)
from .observation import EnvReading, ImageCandidate, ObservationResult
from .prescription import (
    FollowupPlan,
    FollowupTask,
    PesticideItem,
    Prescription,
    PrescriptionAction,
)
from .report import ContactInfo, ReportDecision, ReportSignal
from .triage import ObjectConfirmRequest, TriageResult, match_object_alias

_M = TypeVar("_M", bound=BaseModel)


def dump_contract(model: BaseModel) -> dict:
    """把契约对象压成可 JSON 序列化的字典，供写入 `ConsultationState`。

    固定用 `mode="json"`：state 会经 checkpointer 落库，`date`、`datetime`、枚举都
    必须在这里就变成字符串；若留给 checkpointer 自己处理，序列化行为会随 checkpointer
    实现变化，而历史检查点一旦写入就无法重写。
    """
    return model.model_dump(mode="json")


def load_contract(model_cls: type[_M], payload: dict) -> _M:
    """从 `ConsultationState` 取出并校验回契约对象。

    走过完整的 Pydantic 校验（而不是 `model_construct`）：state 里的 dict 可能来自
    上一个版本写的检查点，字段缺失或取值越界必须在读出时暴露。
    """
    return model_cls.model_validate(payload)


__all__ = [
    "CONFIDENCE_DISPLAY",
    "SKIP_MARKERS",
    "AnswerTypeValue",
    "ContactInfo",
    "DiagnosisResult",
    "EnvReading",
    "EvidenceItem",
    "EvidenceKindValue",
    "EvidenceQuery",
    "EvidenceSet",
    "FollowupPlan",
    "FollowupTask",
    "GateContext",
    "GateResult",
    "GateViolation",
    "ImageCandidate",
    "InterviewBatch",
    "InterviewQuestion",
    "KnowledgeFacts",
    "ObjectConfirmRequest",
    "ObservationResult",
    "PesticideItem",
    "Prescription",
    "PrescriptionAction",
    "QuestionOption",
    "RegionLevelActual",
    "ReportDecision",
    "ReportSignal",
    "TriageResult",
    "UncertaintySource",
    "dump_contract",
    "load_contract",
    "match_object_alias",
]
