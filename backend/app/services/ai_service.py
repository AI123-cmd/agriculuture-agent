"""AI 问诊编排服务：图驱动，落库走 repository，出参为 `ChatResponse`（T2-33）。

本模块整体替换旧实现（只喂一次 state、取一次 `reply` 文本、把 ORM Session 透传进图 config）。
两条不变量：

- **不把数据库会话传进图**：旧代码把 ORM Session 塞进图 config，让图内节点直接访问
  数据库，违反 L2（`app/agent/` 不得导入 `app/models/`）与 L4（不得直接查询数据库）。
  改造后数据由 service 层经 repository 取出，以结构化 `context` 注入。
- **入口注入流程配置事实**：`redlines_loaded` / `required_slots` / `redlines` /
  `config_version` / `config_hash` 是路由函数（`app/agent/graph/routes.py`）与闸门
  （`gate_graph.build_gate_context`）的判据来源。它们的写入方在生产链路中缺失
  （`intake` 只 `state.get("redlines_loaded", False)`），因此必须由本层在会话开始时注入，
  否则每条条件边都会 fail-closed 转人工。
"""

from functools import lru_cache
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.blackboard import progress_of
from app.agent.contracts import match_object_alias, resolve_process_hint
from app.agent.state import ConsultationState, ConsultStage, ProcessId, SlotValue
from app.core.config import get_settings
from app.db.base import new_id
from app.db.uow import unit_of_work
from app.events import EVENT_PRESCRIPTION_CREATED, emit
from app.models import AIConversation, AIMessage, Consultation, ConsultStatus, PrescriptionCard
from app.observability import bound_context, consultation_span
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.models import ProcessConfig
from app.process_config.redlines import RedlineValidator
from app.process_config.version import build_ref
from app.repositories import (
    consult_repo,
    conversation_repo,
    crop_repo,
    message_repo,
    prescription_repo,
    profile_repo,
    tag_repo,
)
from app.schemas.consult import (
    ChatRequest,
    ChatResponse,
    ContactInfoView,
    FollowupPlanView,
    FollowupTaskView,
    HandoffView,
    PesticideItemView,
    PrescriptionActionView,
    PrescriptionView,
    ProgressInfo,
    QuestionView,
)
from app.services.memory_service import MemoryService

# env_source 为这两个值时不允许"没数据就留空"：农户必须知道判断不依赖环境读数。
STALE_ENV_SOURCES = frozenset({"none", "sensor_stale"})

# 环节的口语化文案（技术方案 5.3.1 / 产品说明书 4.2.3）。展示层固定映射，不接受自由文本。
_STAGE_LABELS: dict[ConsultStage, str] = {
    ConsultStage.INTAKE: "正在受理",
    ConsultStage.TRIAGE: "正在判断问题类型",
    ConsultStage.OBSERVE: "正在看现场",
    ConsultStage.INTERVIEW: "正在了解情况",
    ConsultStage.VERIFY: "正在查依据",
    ConsultStage.JUDGE: "正在下判断",
    ConsultStage.PRESCRIBE: "正在给建议",
    ConsultStage.GATE: "正在做安全校验",
    ConsultStage.FOLLOWUP: "正在登记回访",
    ConsultStage.HANDOFF: "已为你转人工",
    ConsultStage.BLOCKED: "出于安全已停止出方",
}

# `HandoffView.reason` 的字面量集合（与技术方案 13.2 一致）。显式标注让 mypy 认可
# `_handoff` 里对 reason 的赋值与改写。
HandoffReason = Literal["out_of_scope", "safety_degrade", "safety_block", "not_converged", "mandatory_report"]

# `HandoffPackage.reason`（四值）到接口 `HandoffView.reason`（五值）的映射。闸门
# `degrade` 与 `block` 在内部同为 `safety_block`，接口层按闸门裁定细分展示。
_SAFETY_DEGRADE: HandoffReason = "safety_degrade"
_SAFETY_BLOCK: HandoffReason = "safety_block"


def stage_label(stage: ConsultStage) -> str:
    """环节 → 面向农户的口语化进度文案。"""
    return _STAGE_LABELS.get(stage, "正在处理")


def build_environment_note(state: ConsultationState) -> str | None:
    """`env_source` 为 none / sensor_stale 时必须返回提示文案，不允许为空。

    环境读数缺失或过期时，处方里的环境相关建议都建立在农户描述之上，产品说明书 4.2.3
    功能点 6 要求界面明确标注这一点。
    """
    env = state.get("observations") or {}
    if env.get("env_source", "none") not in STALE_ENV_SOURCES:
        return None
    region_label = env.get("region_hit_level")
    if region_label:
        return f"暂无本地的环境数据，以下判断基于你的描述；农时提示按{region_label}通用口径给出。"
    return "暂无环境数据，以下判断基于你的描述的情况。"


def render_user_content(state: dict[str, Any], answers: list[Any] | None) -> str:
    """卡片作答没有自由文本，落库与标题需要一句可读摘要：把答案按问题文案拼起来。

    前端提交快捷选项时只发 `answers`、不带 `message`，若直接落库 `req.message` 就会存空串，
    导致历史回看时用户消息一片空白。此处在服务端补上，问题文案取自本轮 `pending_questions`
    （槽位键 → 问法），答不出时记为"不清楚"。
    """
    rows = state.get("pending_questions") or []
    labels = {
        (row.get("slot_key") or row.get("slotKey")): (row.get("text") or "")
        for row in rows
        if (row.get("slot_key") or row.get("slotKey"))
    }
    parts: list[str] = []
    for answer in answers or []:
        key = getattr(answer, "slotKey", "") or ""
        label = labels.get(key) or key
        if not label:
            continue
        value = getattr(answer, "value", "") or ""
        status = getattr(answer, "status", "filled")
        parts.append(f"{label}：{value if status != 'unknown' and value else '不清楚'}")
    return "；".join(parts)


def render_ai_content(response: ChatResponse, state: dict[str, Any]) -> str:
    """追问 / 出方 / 转人工环节的节点不写 `reply`，历史回看需要可读文本，故从结构化结果回填。

    与 `ChatResponse.reply` 刻意分开：实时渲染由前端按 `questions` / `prescription` /
    `handoff` 卡片呈现，这里只保证落库那一条消息不空——历史列表点开后才有内容可看。
    """
    if response.questions:
        texts = [q.text for q in response.questions if q.text]
        if texts:
            return "我需要先了解：" + "；".join(texts)
    if response.prescription and response.prescription.diagnosisSummary:
        return response.prescription.diagnosisSummary
    if response.handoff and response.handoff.summary:
        return response.handoff.summary
    return ""


def build_prescription_view(raw: dict[str, Any]) -> PrescriptionView:
    """把处方契约（或处方卡字段）的 dict 投影成接口视图。

    实时出方读 `state["prescription"]`，历史回看读 `prescription_card` 落库字段——两者
    都是同一套 snake_case 键，抽成一处以免两条路径的字段映射各自漂移。
    """
    plan = raw.get("followup_plan") or {}
    return PrescriptionView(
        diagnosisSummary=raw.get("diagnosis_summary") or "",
        actions=[
            PrescriptionActionView(
                order=action.get("order", 0),
                action=action.get("action") or "",
                timing=action.get("timing") or "",
                method=action.get("method") or "",
                cautions=action.get("cautions") or [],
                stopConditions=action.get("stop_conditions") or [],
            )
            for action in (raw.get("actions") or [])
        ],
        safetyIntervalDays=raw.get("safety_interval_days"),
        maxUsesPerSeason=raw.get("max_uses_per_season"),
        pesticideItems=[
            PesticideItemView(
                name=item.get("name") or "",
                dosageMin=item.get("dosage_min"),
                dosageMax=item.get("dosage_max"),
                dosageUnit=item.get("dosage_unit"),
                safetyIntervalDays=item.get("safety_interval_days"),
                maxUsesPerSeason=item.get("max_uses_per_season"),
                registeredCrops=item.get("registered_crops") or [],
                forbiddenCombinations=item.get("forbidden_combinations") or [],
                sourceNote=item.get("source_note"),
            )
            for item in (raw.get("pesticide_items") or [])
        ],
        followupPlan=FollowupPlanView(
            tasks=[
                FollowupTaskView(
                    dueOffsetDays=task.get("due_offset_days", 0),
                    kind=task.get("kind") or "observe",
                    ask=task.get("ask"),
                )
                for task in (plan.get("tasks") or [])
            ],
            note=plan.get("note"),
        ),
        requiresTechStation=raw.get("requires_tech_station", False),
        detailTerms=raw.get("detail_terms") or {},
    )


def prescription_view_from_card(card: PrescriptionCard) -> PrescriptionView:
    """历史回看：由落库的处方卡还原处方视图。

    与实时出方的差别只有 `cardId` —— 从卡片主键回填，使历史界面里"存入农事台账"按钮
    仍可用（`ledgerSaved` 另由卡片列给出）。
    """
    view = build_prescription_view(
        {
            "diagnosis_summary": card.diagnosis_summary,
            "actions": card.actions or [],
            "pesticide_items": card.pesticide_items or [],
            "safety_interval_days": card.safety_interval_days,
            "max_uses_per_season": card.max_uses_per_season,
            "followup_plan": card.followup_plan or {},
            "requires_tech_station": card.requires_tech_station,
        }
    )
    view.cardId = card.id
    return view



@lru_cache(maxsize=1)
def _alias_configs() -> dict[ProcessId, ProcessConfig]:
    """入口提示归一使用的流程配置集合（含 `object_aliases`）。

    与 `get_triage_agent` 一样按进程内单例缓存：别名是低频变更的配置事实，每次问诊都
    读七份 YAML 是纯浪费。读取口径与 `_inject_config` 的 `get_process_config` 一致，
    都取自 seeds 文件，不受数据库热加载影响。
    """
    return load_all_configs()


def _resolve_process_hint(hint: str | ProcessId | None) -> ProcessId | None:
    """把入口提示归一为流程标识；已是 `ProcessId` 时原样返回，未命中返回 `None`。

    单独抽成模块级函数而不是方法：`_inject_config` 是静态方法（无 `self`），且它的调用
    方既有服务层（传 `ProcessId`），也有测试（直接传「weed」这类字符串），两种形态都要吃下。
    """
    if isinstance(hint, ProcessId):
        return hint
    return resolve_process_hint(hint, _alias_configs())


class AIService:
    async def chat(self, db: AsyncSession, user_id: str, req: ChatRequest) -> ChatResponse:
        # 先定会话主键：归属不符时换新 id，后续持久化与检查点线程都用它（T1-06）。
        session_id = await conversation_repo.resolve_session_id(db, user_id, req.sessionId)

        # 一次问诊 = 一个 trace（T6-06）：根 span 由本层打开，图内节点与模型调用因处于
        # 当前 span 栈内自动成为其后代，无需在图或节点里手工传 parent。日志上下文同步绑定
        # `consultation_id`（T6-08），使该次问诊的全部日志可一次取齐；用 `bound_context`
        # 而非 `bind_context`，退出时只还原本次绑定、不误伤请求级的 `request_id`。
        with consultation_span(session_id, req.processHint), bound_context(consultation_id=session_id):
            state = await self._resume_or_start(db, session_id, user_id, req)
            response = self._build_response(session_id, state)

        # 图执行刻意放在事务之外：把它包进事务会长时间持锁，且检查点写入由
        # AsyncPostgresSaver 自己的连接池管理，与业务事务并非同一连接。
        async with unit_of_work(db, name="ai.chat"):
            await self._persist(db, user_id, session_id, req, response, state=state)

        return response

    async def _resume_or_start(
        self, db: AsyncSession, session_id: str, user_id: str, req: ChatRequest
    ) -> dict[str, Any]:
        """新会话 / 中断恢复 两分支。恢复判据是检查点上是否还有待跑节点。"""
        from app.agent import get_agent, resume_graph  # 延迟导入，避免与 services 形成循环依赖

        graph = await get_agent()
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        if req.sessionId is not None:
            snapshot = await graph.aget_state(config)
            if snapshot.next:  # 中断中，携带答案恢复
                return self._absorb_interrupt(await resume_graph(session_id, self._resume_value(req)))

        # 入口提示归一为流程标识：小程序传的是农户可见作物名（如「水稻」），图内 `intake`
        # 需要用 `ProcessId` 英文值锁定流程。未命中专属流程时写 `None`，让流程不锁定、
        # 交由 `triage` 判定并带 `fallback_reason` 落到 `general`——这比在入口静默兜底更可追溯。
        resolved_hint = _resolve_process_hint(req.processHint)
        initial_state: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "process_hint": resolved_hint.value if resolved_hint is not None else None,
            "farm_plot_id": req.farmEntryId,
            "image_url": req.imageUrl,
            "voice_url": req.voiceUrl,
            "user_message": req.message or "",
            # 作物上下文按农户所选作物的**原始名称**查主作物（`crop_name` 存的就是中文），
            # 与上面归一后的流程标识用途不同，故两处取值刻意分开。
            "context": await self.build_context(db, user_id, req.processHint),
            # 档案快照（T2-14）：图内 `triage` / `candidate_pool` 读的是 `profile` 系列
            # 契约键，而非展示用的 `context`。缺了这一步，`source_hint: [profile]` 的槽位
            # 会被反复追问、对象确认失效，`format_entries` 恒为"无"。
            **await self.build_profile_snapshot(db, user_id),
            "messages": [{"role": "user", "content": req.message or ""}],
        }
        self._inject_config(initial_state, resolved_hint)
        return self._absorb_interrupt(await graph.ainvoke(initial_state, config=config))

    @staticmethod
    def _absorb_interrupt(result: dict[str, Any]) -> dict[str, Any]:
        """把中断载荷摊回 state，供 `_build_response` 统一读取。

        问诊节点在 `interrupt` **之前**暂停，其返回值（`pending_questions` /
        `stage=INTERVIEW`）要等恢复后才合并进 state。因此图暂停那一刻，待答复的问题
        只存在于 `result["__interrupt__"]` 里，不在 state 中——不在此处摊回，接口层就会
        把"该问什么"丢掉，农户收到空问题列表。闸门确认中断同理，只标注环节。
        """
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return result
        payload = getattr(interrupts[0], "value", None)
        if not isinstance(payload, dict):
            return result
        if "questions" in payload:
            result["pending_questions"] = payload.get("questions") or []
            result["stage"] = ConsultStage.INTERVIEW
            if payload.get("stage_hint"):
                result["stage_hint"] = payload["stage_hint"]
        elif payload.get("kind") == "gate_confirm":
            result["stage"] = ConsultStage.GATE
        return result

    @staticmethod
    def _inject_config(state: dict[str, Any], process_hint: str | ProcessId | None) -> None:
        """把流程配置事实注入初始 state，供路由与闸门读取。

        流程未锁定时以 `general` 作基线（与 `triage` 的环境依赖一致）：`process_hint`
        直达则用其对应流程。入口提示经 `_resolve_process_hint` 归一——既接受英文流程标识，
        也接受农户可见的作物名（「水稻」→ rice），使配置注入口径与图内 `intake` 的锁定口径
        完全一致，避免"配置按 rice 注入、路由却按 general 走"这类分叉。未命中专属流程时
        回退 `general`。`redlines_loaded` 取红线校验结果而非恒真——校验不通过时路由据此
        fail-closed 转人工。
        """
        pid = _resolve_process_hint(process_hint) or ProcessId.GENERAL
        config = get_process_config(pid)
        loaded = RedlineValidator().validate(config)
        state["redlines_loaded"] = loaded.loaded
        state["required_slots"] = config.required_slot_keys()
        state["redlines"] = loaded.model_dump(mode="json")
        state["config_version"] = config.version
        state["config_hash"] = build_ref(config).content_hash

    @staticmethod
    def _resume_value(req: ChatRequest) -> dict:
        """中断恢复值：结构化答案走 `answers`（`apply_answers` 两种键写法都接受）。"""
        answers = [answer.model_dump() for answer in (req.answers or [])]
        return {"answers": answers}

    def _build_response(self, session_id: str, state: dict[str, Any]) -> ChatResponse:
        stage = state.get("stage") or ConsultStage.INTAKE
        response = ChatResponse(
            sessionId=session_id,
            stage=stage,
            stageLabel=stage_label(stage),
            progress=ProgressInfo(**progress_of(cast(ConsultationState, state))),
            reply=state.get("reply") or "",
            questions=self._questions(state),
            prescription=self._prescription(state),
            environmentNote=build_environment_note(cast(ConsultationState, state)),
            handoff=self._handoff(state),
            quotaPrompt=self._quota_prompt(state),
            degraded=state.get("degraded", False),
        )
        # questions / handoff / prescription 三者最多出现一个，用断言固定互斥。
        assert sum(x is not None for x in (response.questions, response.handoff, response.prescription)) <= 1
        return response

    @staticmethod
    def _questions(state: dict) -> list[QuestionView] | None:
        """待答复问题。只在问诊环节且 `pending_questions` 非空时返回。"""
        if state.get("stage") != ConsultStage.INTERVIEW:
            return None
        rows = state.get("pending_questions") or []
        if not rows:
            return None
        return [
            QuestionView(
                slotKey=row.get("slot_key") or row.get("slotKey") or "",
                text=row.get("text") or "",
                why=row.get("why") or "",
                options=row.get("options"),
                answerType=row.get("answer_type") or row.get("answerType") or "text",
                allowUnknown=True,
            )
            for row in rows
        ]

    @staticmethod
    def _prescription(state: dict) -> PrescriptionView | None:
        """处方卡。转人工或已被闸门抑制时为 `None`（与 handoff 互斥）。"""
        if state.get("handoff_package") is not None or state.get("stage") == ConsultStage.HANDOFF:
            return None
        if state.get("prescription_suppressed"):
            return None
        raw = state.get("prescription")
        if not raw:
            return None
        return build_prescription_view(raw)

    def _handoff(self, state: dict[str, Any]) -> HandoffView | None:
        """转人工信息。非空时处方必为 `None`（由 `_prescription` 保证）。"""
        package = state.get("handoff_package")
        if not package:
            return None
        gate = state.get("gate_result") or {}
        reason: HandoffReason = package.get("reason") or "not_converged"
        if reason == "legal_report":
            reason = "mandatory_report"
        elif reason == "safety_block":
            reason = _SAFETY_DEGRADE if gate.get("decision") == "degrade" else _SAFETY_BLOCK
        return HandoffView(
            reason=reason,
            summary=package.get("headline") or package.get("problem_summary") or "",
            confirmedInfo=[f"{key}：{value}" for key, value in (package.get("confirmed_facts") or {}).items()],
            observationPoints=[item.get("text") or "" for item in (package.get("observations") or [])],
            excludedItems=package.get("excluded") or [],
            contacts=[
                ContactInfoView(
                    kind=contact.get("kind") or "agri_station",
                    name=contact.get("name") or "",
                    phone=contact.get("phone"),
                    regionCode=contact.get("region_code"),
                    note=contact.get("note"),
                )
                for contact in (package.get("contacts") or [])
            ],
            speakable=True,
        )

    @staticmethod
    def _quota_prompt(state: dict) -> None:
        """配额提示。配额来源（服务与表）尚未接入，恒为 `None`（T2-33 只留接口位）。"""
        return None

    async def _persist(
        self,
        db: AsyncSession,
        user_id: str,
        session_id: str,
        req: ChatRequest,
        response: ChatResponse,
        *,
        state: dict,
    ) -> None:
        """落库本轮对话（不提交，事务边界由 `chat` 的 `unit_of_work` 统一收口）。

        `flush` 不能省：会话工厂是 `autoflush=False`，新建的会话行若未下推，
        `message_repo.add_many` 的归属校验会查不到父会话而误报越权。

        产出处方时额外落库 `consultation` 与 `prescription_card`，并把卡片主键回填进
        `response.prescription.cardId`——响应在进入本事务前已构建，回填须在提交前完成，
        否则前端拿不到 `cardId`、入台账按钮无从调用（§3.4）。
        """
        message = req.message or ""
        # 卡片作答路径前端只发 `answers`、不带 `message`；追问/出方/转人工环节图节点也不写
        # `reply`。两类内容都需在落库前补齐可读文本，否则历史回看时消息为空白。
        stored_user = message or render_user_content(state, req.answers)
        stored_ai = response.reply or render_ai_content(response, state)

        conv = await conversation_repo.get(db, user_id, session_id)
        if conv is None:
            conv = AIConversation(id=session_id, title=stored_user[:15] or "新对话")
            await conversation_repo.add(db, user_id, conv)
            await db.flush()

        messages = await message_repo.add_many(
            db,
            user_id,
            session_id,
            [
                AIMessage(role="user", content=stored_user, image_url=req.imageUrl),
                AIMessage(role="ai", content=stored_ai),
            ],
        )

        await MemoryService().remember(db, user_id, session_id, stored_user, stored_ai)

        # 判据用 `response.prescription`（构建响应时的最终产物）而非 `state["stage"]`：
        # 图成功路径的终点 stage 已被 `followup` 节点改写为 FOLLOWUP，用 stage==PRESCRIBE
        # 判定会恒假，导致卡片不落库、事件不发出（§3.4 的根因）。
        if response.prescription is not None:
            card = await self._persist_prescription(
                db, user_id, session_id, request_message=stored_user, response=response, state=state
            )
            await self._emit_prescription_created(db, user_id, session_id, messages[-1], card_id=card.id)

    async def _persist_prescription(
        self,
        db: AsyncSession,
        user_id: str,
        session_id: str,
        *,
        request_message: str,
        response: ChatResponse,
        state: dict,
    ) -> PrescriptionCard:
        """落库问诊会话与处方卡，回填 `cardId` 后返回卡片。

        `prescription_card.consultation_id` 是 NOT NULL 外键指向 `consultation.id`，且本链路
        此前从不建 `consultation` 行，故首次产出处方时必须先建会话行（主键复用 session_id，
        与 `AIConversation` 同键）。会话已存在（续问后再次开方）则只滚动 stage / status /
        rounds / slots_snapshot 这几项随轮次推进的字段，不重建。
        """
        assert response.prescription is not None  # 调用方已保证
        stage = state.get("stage") or ConsultStage.INTAKE
        process_value = self._process_value(state)
        triage = state.get("triage") or {}
        object_label = state.get("object_label") or triage.get("object_label") or ""
        budget = state.get("budget") or {}
        rounds = int(budget.get("interview_rounds", 0) or 0)
        slots_snapshot = self._slots_snapshot(state)

        consult = await consult_repo.get(db, user_id, session_id)
        if consult is None:
            consult = Consultation(
                id=session_id,
                farm_entry_id=state.get("farm_plot_id"),
                process_id=process_value,
                process_locked=bool(state.get("process_locked", False)),
                title=request_message[:15] or "新对话",
                # NOT NULL 且列宽有限，超长即截断：展示名与摘要都是给人看的短文本。
                object_label=(object_label or "未确认对象")[:60],
                topic_summary=(request_message or response.reply)[:120],
                stage=stage.value,
                rounds=rounds,
                # 产出处方即"有结论但未闭环"，置为观察期，供 T3-04 的首页跟进提示取数
                # （`list_observing` 会再排除已登记回访任务的会话，两处合起来才不重复）。
                status=ConsultStatus.OBSERVING.value,
                triage_snapshot=triage,
                slots_snapshot=slots_snapshot,
            )
            await consult_repo.add(db, user_id, consult)
            await db.flush()
        else:
            await consult_repo.update_fields(
                db,
                user_id,
                session_id,
                stage=stage.value,
                status=ConsultStatus.OBSERVING.value,
                rounds=rounds,
                slots_snapshot=slots_snapshot,
            )

        raw = state.get("prescription") or {}
        card = PrescriptionCard(
            id=new_id("card"),
            consultation_id=session_id,
            diagnosis=state.get("diagnosis") or {},
            actions=raw.get("actions") or [],
            pesticide_items=raw.get("pesticide_items") or [],
            safety_interval_days=raw.get("safety_interval_days"),
            max_uses_per_season=raw.get("max_uses_per_season"),
            followup_plan=raw.get("followup_plan"),
            facts_provenance=raw.get("facts_provenance") or {},
            gate_result=state.get("gate_result") or {},
            gate_config_version=int(state.get("config_version") or 0),
            requires_tech_station=bool(raw.get("requires_tech_station", False)),
            process_id=process_value,
            diagnosis_summary=raw.get("diagnosis_summary"),
        )
        await prescription_repo.add(db, user_id, card)
        await db.flush()  # 卡片须先落库，回访消费者才能按 consultation_id 反查到

        response.prescription.cardId = card.id
        return card

    @staticmethod
    def _process_value(state: dict) -> str:
        """归一流程标识为落库字符串：`state["process_id"]` 优先，回落到 `process_hint`。

        `process_id` 由分诊或入口注入为 `ProcessId`，但中断恢复后的检查点可能给出同值字符串；
        两者都吃下，都没有时用 `general` 兜底（`process_id` 列 NOT NULL）。
        """
        raw = state.get("process_id") or state.get("process_hint")
        if isinstance(raw, ProcessId):
            return raw.value
        return str(raw) if raw else ProcessId.GENERAL.value

    @staticmethod
    def _slots_snapshot(state: dict) -> dict:
        """把 `state["slots"]` 序列化成可 JSON 存储的 dict。

        `slots` 里是 `SlotValue` 对象（`extra="forbid"`），不能直接进 JSONB 列；检查点
        反序列化后可能已是同形 dict，故两种形态都要能过。
        """
        slots = state.get("slots") or {}
        return {
            key: slot.model_dump(mode="json") if isinstance(slot, SlotValue) else slot
            for key, slot in slots.items()
        }

    async def _emit_prescription_created(
        self, db: AsyncSession, user_id: str, session_id: str, reply_message: AIMessage, *, card_id: str
    ) -> None:
        """把"处方已生成"写进发件箱（T1-13）。必须在 `unit_of_work` 内调用。

        事件与业务写在同一事务里，因此不存在"业务提交了但事件没落库"。聚合根取处方卡主键：
        卡片已在本事务内落库，按轮次天然唯一（一次开方一张卡），且消费者可按 id 直接回查。

        载荷带 `user_id` 与 `session_id`：T3-09 的回访消费者按 `session_id` 反查处方卡，而反查
        是归属强制的（`consult_repo.get_card_by_consultation`）。投递端只把 `payload` 交给 ARQ
        任务、不带事件行的 `user_id` 列，故这里显式写进载荷，消费者无需回查发件箱。
        """
        await db.flush()
        await emit(
            db,
            event_type=EVENT_PRESCRIPTION_CREATED,
            aggregate_type="prescription",
            aggregate_id=card_id,
            user_id=user_id,
            payload={
                "session_id": session_id,
                "card_id": card_id,
                "message_id": reply_message.id,
                "user_id": user_id,
                "summary": (reply_message.content or "")[:200],
            },
        )

    async def build_context(self, db: AsyncSession, user_id: str, process_hint: str | None) -> dict:
        """组装诊断上下文。图内智能体只与这份结构化 dict 交互，不查库（L2/L4）。

        四次查询分属不同表，无法合并；且同一个 `AsyncSession` 不是并发安全的，因此顺序
        执行、不能改成 `asyncio.gather`。
        """
        profile = await profile_repo.first(db, user_id)
        crop = await crop_repo.main_crop(db, user_id, process_hint)
        tag = await tag_repo.first(db, user_id)

        def mask(name: str) -> str:
            return (name[0] + "**") if name else "农户"

        return {
            "farmer": {
                "name": mask(profile.real_name) if profile else "农户",
                "location": (
                    f"{profile.province or ''}{profile.city or ''}{profile.district or ''}" if profile else ""
                ),
            },
            "crop": {
                "name": crop.crop_name if crop else (process_hint or "通用农业"),
                "growthStage": crop.growth_stage if crop else "",
                "plantMu": float(crop.plant_mu) if crop else None,
            },
            "profile_tags": tag.tags if tag and tag.tags else [],
        }

    async def build_profile_snapshot(self, db: AsyncSession, user_id: str) -> dict:
        """组装图内契约要求的档案快照（T2-14），是"档案 → 问诊图"的唯一接通点。

        图内 `triage`（对象确认、降级标签）与候选池筛选器读的是 `profile` /
        `profile_process_id` / `profile_process_hint` 三个键，而不是展示用的 `context`。
        缺了本方法，`source_hint: [profile]` 的槽位会被反复追问、对象确认失效，
        分诊提示词里的经营条目也恒为"无"。

        `slot_values` 的键必须与目标流程 `required_slots` 的 key 一致，否则筛选器
        （`_collect_auto_facts`）无法据此剔除候选。填充口径是"档案里的值确实落在该槽位
        声明的 `options` 内"——`Crop.growth_stage` 存的是农户在档案页选的阶段词，恰好能对上
        各流程的阶段槽位（`rice_growth_stage` / `pig_growth_stage` 等）。其余槽位（最近用药、
        上次采摘等）在用药史/农事记录落地前一律不写入——宁可多问一句，也不写入伪造值。

        `prescription_history` 显式给空列表：它表示"暂无已知用药史"这一事实，而非占位。
        """
        profile = await profile_repo.first(db, user_id)
        crops = await crop_repo.list_for_profile(db, user_id)
        configs = _alias_configs()

        entries: list[dict] = []
        for crop in crops:
            pid = match_object_alias(crop.crop_name or "", configs)
            entries.append(
                {
                    "process_id": pid.value if pid is not None else None,
                    "start_date": crop.sowing_date.isoformat() if crop.sowing_date else None,
                    "display_name": crop.crop_name or "",
                }
            )

        # 主条目：`list_for_profile` 已按"主作物优先"排序，取首条即档案的代表作物。
        main_crop = crops[0] if crops else None
        main_pid = match_object_alias(main_crop.crop_name or "", configs) if main_crop is not None else None

        slot_values: dict[str, str] = {}
        stage = (main_crop.growth_stage or "").strip() if main_crop is not None else ""
        if main_pid is not None and stage:
            for spec in get_process_config(main_pid).required_slots:
                if "profile" in spec.source_hint and spec.options and stage in spec.options:
                    slot_values[spec.key] = stage

        object_label = ""
        if main_crop is not None:
            label_parts = [main_crop.crop_name or ""]
            if main_crop.plant_mu:
                label_parts.append(f"{float(main_crop.plant_mu):g}亩")
            object_label = "·".join(part for part in label_parts if part)

        location = f"{profile.province or ''}{profile.city or ''}{profile.district or ''}" if profile else ""
        # 无档案且无作物：`profile` 给空字典，使 `triage.degrade` 的 `profile_ready=bool(profile)`
        # 如实为假，而不是被一堆空键误判为"档案已就绪"。
        snapshot = {} if profile is None and not entries else {
            "location": location,
            "entries": entries,
            "object_label": object_label,
            "slot_values": slot_values,
        }
        return {
            "profile": snapshot,
            "profile_process_id": main_pid.value if main_pid is not None else None,
            "profile_process_hint": main_pid.value if main_pid is not None else None,
            "prescription_history": [],
        }

    async def history(self, db: AsyncSession, user_id: str, page: int = 1, size: int = 20) -> dict:
        """历史列表：固定 2 次查询（COUNT + 分页带预览），预览与消息数由同一条 SQL 带出。"""
        settings = get_settings()
        size = max(1, min(size, settings.HISTORY_MAX_PAGE_SIZE))
        total = await conversation_repo.count(db, user_id)

        fetch = (
            conversation_repo.paginate_with_preview_lateral
            if settings.HISTORY_QUERY_STRATEGY == "lateral"
            else conversation_repo.paginate_with_preview
        )
        rows = await fetch(db, user_id, page=max(1, page), size=size)

        return {
            "list": [
                {
                    "id": conv.id,
                    "title": conv.title or "新对话",
                    "preview": preview or "",
                    "messageCount": msg_count,
                    "updatedAt": int(conv.update_time.timestamp() * 1000) if conv.update_time else 0,
                }
                for conv, msg_count, preview in rows
            ],
            "total": total,
        }

    async def delete_history(self, db: AsyncSession, user_id: str, session_id: str) -> None:
        """删除会话及其消息：归属校验在仓储内完成，不属于当前用户抛 403。"""
        async with unit_of_work(db, name="ai.delete_history"):
            await conversation_repo.delete_with_messages(db, user_id, session_id)

    async def conversation_detail(self, db: AsyncSession, user_id: str, session_id: str) -> dict:
        """回看单个会话：归属不符抛 403（与删除同口径，不区分"不存在"与"不属于你"）。

        除消息文本外还带出**最新一张处方卡**：历史界面要与实时界面一致地展示处方结论，
        而消息表只存文本（`ai_message.content`），处方结构落在 `prescription_card`。
        转人工会话没有卡片，`prescription` 为 `None`，前端回看时也不渲染处方卡。
        """
        conv = await conversation_repo.get_or_raise(db, user_id, session_id)
        rows = await message_repo.list_by_conversation(db, user_id, session_id)
        card = await prescription_repo.get_latest_by_consultation(db, user_id, session_id)
        return {
            "sessionId": conv.id,
            "title": conv.title or "新对话",
            "messages": [
                {"role": msg.role, "content": msg.content, "imageUrl": msg.image_url} for msg in rows
            ],
            "prescription": (
                prescription_view_from_card(card).model_dump(mode="json") if card is not None else None
            ),
            "ledgerSaved": bool(card.ledger_saved) if card is not None else False,
        }


__all__ = ["AIService", "build_environment_note", "stage_label"]
