"""问诊接口契约（技术方案 13.2）。

本模块是 T2-33 的出入参定义：`ChatRequest` 收口新会话 / 中断恢复两类入参，
`ChatResponse` 承载图驱动后的完整产出（`stage` / `progress` / `questions` /
`prescription` / `environmentNote` / `handoff` / `quotaPrompt` / `degraded`）。

视图模型（`*View`）是内部契约到接口字段的投影：字段名走 camelCase、只暴露前端
渲染所需的部分，内部审计字段（如药剂条目的 `knowledge_id`、处方的
`facts_provenance`）不进入响应。
"""

from typing import Literal

from pydantic import BaseModel

from app.agent.state import ConsultStage


class SlotAnswer(BaseModel):
    """结构化快捷选项回答。

    `status` 只用 `filled` / `unknown`：把"农户明确答不出"与"尚未询问"（`missing`）
    分开——`missing` 不出现在请求里，只出现在服务端 state 的 `slots` 中。
    """

    slotKey: str
    value: str
    status: Literal["filled", "unknown"] = "filled"


class ChatRequest(BaseModel):
    """问诊入参。全部可选：新会话可只带 `message`，恢复时带 `sessionId` + `answers`。"""

    sessionId: str | None = None
    farmEntryId: str | None = None
    message: str | None = None
    imageUrl: str | None = None
    voiceUrl: str | None = None
    processHint: str | None = None
    answers: list[SlotAnswer] | None = None


class ProgressInfo(BaseModel):
    current: int
    total: int


class QuestionView(BaseModel):
    slotKey: str
    text: str
    why: str
    options: list[str] | None = None
    answerType: Literal["single", "multi", "text", "photo", "number"]
    allowUnknown: bool = True


class PrescriptionActionView(BaseModel):
    order: int
    action: str
    timing: str
    method: str
    cautions: list[str]
    stopConditions: list[str]


class PesticideItemView(BaseModel):
    name: str
    dosageMin: float | None = None
    dosageMax: float | None = None
    dosageUnit: str | None = None
    safetyIntervalDays: int | None = None
    maxUsesPerSeason: int | None = None
    registeredCrops: list[str] = []
    forbiddenCombinations: list[str] = []
    sourceNote: str | None = None


class FollowupTaskView(BaseModel):
    dueOffsetDays: int
    kind: Literal["effect_check", "risk_check", "preserve_scene", "observe", "compliance_check"]
    ask: str | None = None


class FollowupPlanView(BaseModel):
    tasks: list[FollowupTaskView] = []
    note: str | None = None


class PrescriptionView(BaseModel):
    diagnosisSummary: str
    actions: list[PrescriptionActionView]
    safetyIntervalDays: int | None = None
    maxUsesPerSeason: int | None = None
    pesticideItems: list[PesticideItemView] = []
    followupPlan: FollowupPlanView
    requiresTechStation: bool = False
    detailTerms: dict[str, str] = {}


class ContactInfoView(BaseModel):
    kind: Literal["vet_station", "agri_station", "authority", "hotline"]
    name: str
    phone: str | None = None
    regionCode: str | None = None
    note: str | None = None


class HandoffView(BaseModel):
    reason: Literal[
        "out_of_scope",
        "safety_degrade",
        "safety_block",
        "not_converged",
        "mandatory_report",
    ]
    summary: str
    confirmedInfo: list[str] = []
    observationPoints: list[str] = []
    excludedItems: list[str] = []
    contacts: list[ContactInfoView] = []
    speakable: bool = True


class QuotaPromptView(BaseModel):
    scene: str
    text: str
    granted: bool = False


class ChatResponse(BaseModel):
    sessionId: str
    stage: ConsultStage
    stageLabel: str
    progress: ProgressInfo
    reply: str
    questions: list[QuestionView] | None = None
    prescription: PrescriptionView | None = None
    environmentNote: str | None = None
    handoff: HandoffView | None = None
    quotaPrompt: QuotaPromptView | None = None
    degraded: bool = False


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ContactInfoView",
    "FollowupPlanView",
    "FollowupTaskView",
    "HandoffView",
    "PesticideItemView",
    "PrescriptionActionView",
    "PrescriptionView",
    "ProgressInfo",
    "QuestionView",
    "QuotaPromptView",
    "SlotAnswer",
]
