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

from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.blackboard import progress_of
from app.agent.state import ConsultationState, ConsultStage, ProcessId
from app.core.config import get_settings
from app.db.uow import unit_of_work
from app.events import EVENT_PRESCRIPTION_CREATED, emit
from app.models import AIConversation, AIMessage
from app.process_config.loader import get_process_config
from app.process_config.redlines import RedlineValidator
from app.process_config.version import build_ref
from app.repositories import conversation_repo, crop_repo, message_repo, profile_repo, tag_repo
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


class AIService:
    async def chat(self, db: AsyncSession, user_id: str, req: ChatRequest) -> ChatResponse:
        # 先定会话主键：归属不符时换新 id，后续持久化与检查点线程都用它（T1-06）。
        session_id = await conversation_repo.resolve_session_id(db, user_id, req.sessionId)

        state = await self._resume_or_start(db, session_id, user_id, req)
        response = self._build_response(session_id, state)

        # 图执行刻意放在事务之外：把它包进事务会长时间持锁，且检查点写入由
        # AsyncPostgresSaver 自己的连接池管理，与业务事务并非同一连接。
        async with unit_of_work(db, name="ai.chat"):
            await self._persist(db, user_id, session_id, req, response.reply, state=state)

        return response

    async def _resume_or_start(self, db: AsyncSession, session_id: str, user_id: str, req: ChatRequest) -> dict[str, Any]:
        """新会话 / 中断恢复 两分支。恢复判据是检查点上是否还有待跑节点。"""
        from app.agent import get_agent, resume_graph  # 延迟导入，避免与 services 形成循环依赖

        graph = await get_agent()
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        if req.sessionId is not None:
            snapshot = await graph.aget_state(config)
            if snapshot.next:  # 中断中，携带答案恢复
                return self._absorb_interrupt(await resume_graph(session_id, self._resume_value(req)))

        initial_state: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "process_hint": req.processHint,
            "farm_plot_id": req.farmEntryId,
            "image_url": req.imageUrl,
            "voice_url": req.voiceUrl,
            "user_message": req.message or "",
            "context": await self.build_context(db, user_id, req.processHint),
            "messages": [{"role": "user", "content": req.message or ""}],
        }
        self._inject_config(initial_state, req.processHint)
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
    def _inject_config(state: dict[str, Any], process_hint: str | None) -> None:
        """把流程配置事实注入初始 state，供路由与闸门读取。

        流程未锁定时以 `general` 作基线（与 `triage` 的环境依赖一致）：`process_hint`
        直达则用其对应流程。`redlines_loaded` 取红线校验结果而非恒真——校验不通过时
        路由据此 fail-closed 转人工。
        """
        pid = ProcessId.GENERAL
        if process_hint:
            try:
                pid = ProcessId(process_hint)
            except ValueError:
                pid = ProcessId.GENERAL
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
        reply: str,
        *,
        state: dict,
    ) -> None:
        """落库本轮对话（不提交，事务边界由 `chat` 的 `unit_of_work` 统一收口）。

        `flush` 不能省：会话工厂是 `autoflush=False`，新建的会话行若未下推，
        `message_repo.add_many` 的归属校验会查不到父会话而误报越权。
        """
        message = req.message or ""
        conv = await conversation_repo.get(db, user_id, session_id)
        if conv is None:
            conv = AIConversation(id=session_id, title=message[:15] or "新对话")
            await conversation_repo.add(db, user_id, conv)
            await db.flush()

        messages = await message_repo.add_many(
            db,
            user_id,
            session_id,
            [
                AIMessage(role="user", content=message, image_url=req.imageUrl),
                AIMessage(role="ai", content=reply),
            ],
        )

        await MemoryService().remember(db, user_id, session_id, message, reply)

        if state.get("stage") == ConsultStage.PRESCRIBE and not state.get("prescription_suppressed"):
            await self._emit_prescription_created(db, user_id, session_id, messages[-1])

    async def _emit_prescription_created(
        self, db: AsyncSession, user_id: str, session_id: str, reply_message: AIMessage
    ) -> None:
        """把"处方已生成"写进发件箱（T1-13）。必须在 `unit_of_work` 内调用。

        事件与业务写在同一事务里，因此不存在"业务提交了但事件没落库"。聚合根取本条
        AI 回复消息：处方目前还没有独立表，而"一次对话产生的这份处方"正是回复消息本身，
        用它做 `aggregate_id`/幂等键天然按轮次唯一，且消费者可直接按 id 回查回复原文。
        """
        await db.flush()
        await emit(
            db,
            event_type=EVENT_PRESCRIPTION_CREATED,
            aggregate_type="prescription",
            aggregate_id=str(reply_message.id),
            user_id=user_id,
            payload={
                "session_id": session_id,
                "message_id": reply_message.id,
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


__all__ = ["AIService", "build_environment_note", "stage_label"]
