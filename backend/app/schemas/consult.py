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
    # 处方卡主键。问诊落库产出 `PrescriptionCard` 后回填，前端据此调用
    # `POST /api/ai/prescription/{cardId}/ledger` 把处方存入农事台账（T3-15）。
    # 卡片未落库（如落库前构建响应）时为 None——按钮以 `wx:if="{{prescription.cardId}}"` 控制显隐，
    # 缺省不渲染，因此加在嵌套视图而非 `ChatResponse` 顶层。
    cardId: str | None = None
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


# 统一的"问诊结果"类型。四条终态路径各对应一个取值，前端据它决定卡片内部区块：
# prescription 出方 / handoff 转人工 / blocked 安全拦截 / report 法定报告。
ConsultResultKind = Literal["prescription", "handoff", "blocked", "report"]


class ConsultResultView(BaseModel):
    """统一"问诊结果"。所有终态路径都产出，转人工路径也不例外。

    出方路径给出处置建议（`actions` / `pesticideItems` / `followupPlan`），转人工路径给出
    结论 + 已了解情况 + 观察要点 + 下一步建议（`conclusion` / `confirmedInfo` /
    `observationPoints` / `nextStep` / `contacts`）。两类共用同一张卡，前端只渲染 `result`，
    不再区分"处方卡"与"转人工卡"。合规红线（安全拦截不复述药剂、法定报告不做认定）在
    服务层的构造函数里落实。
    """

    kind: ConsultResultKind
    title: str = "问诊结果"
    conclusion: str
    confidenceLabel: str | None = None
    basis: list[str] = []
    confirmedInfo: list[str] = []
    observationPoints: list[str] = []
    excludedItems: list[str] = []
    actions: list[PrescriptionActionView] = []
    pesticideItems: list[PesticideItemView] = []
    followupPlan: FollowupPlanView | None = None
    # 有序的"该做的事"清单：法定报告情形承载强制动作（顺序即展示顺序，不截断）。
    steps: list[str] = []
    nextStep: str | None = None
    contacts: list[ContactInfoView] = []
    requiresTechStation: bool = False
    speakable: bool = False
    cardId: str | None = None


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
    # 统一"问诊结果"：终态（出方 / 转人工 / 安全拦截 / 法定报告）非空，追问轮为空。
    # 前端以它作为唯一渲染源；`prescription` / `handoff` 暂留一个版本作过渡。
    result: ConsultResultView | None = None
    degraded: bool = False


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ConsultResultKind",
    "ConsultResultView",
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
