"""闸门子图：state → GateContext → 判定 → 三态输出处理（T2-45，技术方案 8.1）。

本模块是「生成与校验物理隔离」在编排层的落点：闸门是图上一个独立子图，**唯一**允许读
`ConsultationState` 的地方是 `build_gate_context`，它把 state 压成 `GateContext`，之后
`GateService.review` 与全部规则只接触这份契约。刻意不读 `messages` 与 `reply`：闸门不接收
会话消息，因此无法被对话中的表述影响；多读一个字段就等于多一个被影响的入口。

三个 decision 的输出处理（产品说明书 6.2、技术方案 5.3.6）：

- `allow`   放行处方卡，原文本保留，`stage=GATE`；
- `degrade` 输出**整体替换**为 `degrade_output`，不改写后输出，`stage=GATE`；
- `block`   拒绝输出，`stage=BLOCKED`（**不是** `HANDOFF`），并触发知识条目复核工单。

`degrade` 与 `block` 都用 `degrade_output` 作为对外文案：农户不需要知道"是硬红线还是地区
不匹配"，两者的正确指引都是"问本地农技站"，差别只在内部审计与工单。

依赖方向：本模块在 `app.agent` 下，导入 `app.gate`/`app.repositories` 合法（L1 只禁止
gate → agent）；不导入 `app.models`（L2）；不直接 `db.query`（L4，工单入队走仓储）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph, StateNode
from langgraph.types import interrupt

from app.agent.contracts import Prescription, dump_contract
from app.agent.state import ConsultationState, ConsultStage, ProcessId
from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.contracts import GateContext, GatePayload, GatePrescription, GateResult, MedicationRecord
from app.gate.enums import ProcessId as GateProcessId
from app.gate.ports import KnowledgeEntrySnapshot, KnowledgeFacts
from app.gate.service import GateService
from app.observability import traced_node
from app.process_config.redlines import LoadedRedlines
from app.repositories.gate_audit import enqueue_review_ticket

# 闸门节点的形态：与主图其余节点一致，只吃 state、返回 state 增量。用 LangGraph 的节点协议
# 标注，`add_node` 才能接受工厂产出的函数（mypy 无法把 `Callable` 别名匹配到节点协议）。
GateNode = StateNode[ConsultationState, None]


class _EmptyKnowledgeFacts:
    """未接入知识库时的闸门事实源占位：查不到条目、未核准、无登记作物。

    返回"查不到"而非编造数据：`R-WHITELIST` 因拿不到条目而拦截，是 fail-closed 的正确
    方向。形态与 `factories._EmptyKnowledge` 的占位一致，但那个暴露的是检索用的 `search`，
    并非闸门的 `KnowledgeFacts` 协议，因此闸门需要一个自己的空实现。
    """

    def get_entry(self, knowledge_id: str) -> KnowledgeEntrySnapshot | None:
        return None

    def is_approved(self, knowledge_id: str) -> bool:
        return False

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ()


def _flatten_slots(slots: dict[str, Any]) -> dict[str, str | date | None]:
    """只取已确认的值。`unknown` 与 `missing` 一律落成 `None`，让规则走保守分支。

    `unknown`（问过且答不出）与 `missing`（还没问）在问诊层必须区分，但闸门层不区分：
    二者都无法证明方案不冲突，故统一归为"缺失"。`R-INTERVAL` 因此在两种情形下都判定
    "缺少下一轮采摘计划"并 block，这是 fail-closed 的正确方向（全局约定第四节）。
    """
    out: dict[str, str | date | None] = {}
    for key, slot in slots.items():
        raw = slot if isinstance(slot, Mapping) else slot.model_dump()
        out[key] = raw.get("value") if raw.get("status") == "filled" else None
    return out


def _history(rows: list[dict]) -> list[MedicationRecord]:
    """把档案用药史摊平成 `MedicationRecord`。

    档案快照的字段形态由档案模型（T2-14）决定，此处不假设固定键名：按常见键名取值，
    `product`/`name`/`pesticide_name` 任一存在即视为药剂名，`date`/`used_on` 任一存在即
    视为用药日期。缺名或缺日期（含解析失败）的行跳过——无法确定用药时点的记录无法参与
    "每季"窗口统计，塞进来只会得到错误结论。
    """
    out: list[MedicationRecord] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get("product") or row.get("pesticide_name") or row.get("name")
        raw_date = row.get("used_on") or row.get("date")
        if not name or not raw_date:
            continue
        try:
            used_on = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        knowledge_id = row.get("knowledge_id")
        out.append(
            MedicationRecord(
                knowledge_id=str(knowledge_id) if knowledge_id else None,
                pesticide_name=str(name),
                used_on=used_on,
            )
        )
    return out


def _season_start(state: ConsultationState) -> date | None:
    """每季次数统计的起点。当前 state 契约无此键，故缺省返回 `None`。

    返回 `None` 表示不做季节窗口过滤、全量计入用药史，是保守方向（计入越多越可能命中
    `R-MAX-USE` 上限）。若未来 intake 写入该键，此处即自动生效。
    """
    raw = state.get("season_start")
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


# 数值型红线载荷的保守合并方向：上限取下限更小的（更严），下限取更大的（更严）。
_UPPER_BOUND_KEYS = frozenset({"limit", "max"})
_LOWER_BOUND_KEYS = frozenset({"min_days", "min"})


def _merge_payload(key: str, current: object, incoming: object) -> object:
    """同一 rule_id 被多条红线声明时的合并：数值边界取更严的一侧，其余保持先到者。"""
    if key in _UPPER_BOUND_KEYS and isinstance(current, (int, float)) and isinstance(incoming, (int, float)):
        return min(current, incoming)
    if key in _LOWER_BOUND_KEYS and isinstance(current, (int, float)) and isinstance(incoming, (int, float)):
        return max(current, incoming)
    return current


def _rule_configs(redlines: LoadedRedlines) -> dict[str, dict[str, object]]:
    """把红线载荷摊平成 `rule_id → data`，供规则读取 `limit`/`min_days`/`regions` 等配置。

    同一 rule_id 可能被多条红线声明（如两条都约束 `R-INTERVAL`），此时按保守方向合并，
    避免"后一条覆盖前一条"把更严的约束丢掉。
    """
    merged: dict[str, dict[str, object]] = {}
    for redline in redlines.redlines:
        for rule_id in redline.rule_ids:
            bucket = merged.setdefault(rule_id.value, {})
            for key, value in redline.payload.items():
                bucket[key] = _merge_payload(key, bucket[key], value) if key in bucket else value
    return merged


def build_gate_context(state: ConsultationState, *, knowledge: KnowledgeFacts, today: date) -> GateContext:
    """把 state 压成 `GateContext`。这是闸门侧唯一允许读 state 的地方。

    刻意不读 `messages` 与 `reply`：8.1 要求闸门不接收会话消息，因此无法被对话中的表述
    影响。多读一个字段就等于多一个被影响的入口。
    """
    prescription = Prescription.model_validate(state["prescription"])
    # 红线快照缺失时构造"不可用"而非抛错：让引擎的 R-REDLINE-LOAD 按 fail-closed 判 block，
    # 比在节点里抛异常中断整张图更符合闸门的失败语义。
    raw_redlines = state.get("redlines")
    if raw_redlines:
        redlines = LoadedRedlines.model_validate(raw_redlines)
    else:
        # `LoadedRedlines.process_id` 用的是 `app.agent.state.ProcessId`（红线模块反向依赖状态层），
        # 而 `GateContext.process_id` 用的是闸门侧镜像枚举，两处各取其源，故分别转换。
        redlines = LoadedRedlines.unavailable(
            ProcessId(state["process_id"]), state.get("config_version") or 0, "state 未携带红线快照"
        )
    return GateContext(
        session_id=state["session_id"],
        user_id=state["user_id"],
        process_id=GateProcessId(state["process_id"]),
        config_version=state.get("config_version", 0),
        prescription=GatePrescription.from_contract(prescription),
        payload=GatePayload(rendered_text=state.get("prescription_text") or ""),
        redlines=redlines,
        slots=_flatten_slots(state.get("slots") or {}),
        knowledge=knowledge,
        medication_history=_history(state.get("prescription_history") or []),
        region_code=state.get("region_code"),
        today=today,
        season_start=_season_start(state),
        rule_configs=_rule_configs(redlines),
    )


async def _raise_review_ticket(ctx: GateContext, result: GateResult) -> None:
    """block 事件触发知识条目复核工单（8.5 第一条复盘用途）。

    工单只记录必要字段，不落会话原文。落库走 repositories，不直接 `db.query`（L4）。
    带 `knowledge_ids` 是 8.5 第一条复盘用途的关键：它让复核人员能直接定位"是这几条知识
    条目的数据有问题"还是"模型越界"。
    """
    await enqueue_review_ticket(
        process_id=ctx.process_id.value,
        config_version=ctx.config_version,
        rule_ids=sorted({v.rule_id for v in result.violations}),
        knowledge_ids=sorted({i.knowledge_id for i in ctx.prescription.pesticide_items}),
        decision=result.decision,
    )


def build_gate_node(knowledge: KnowledgeFacts, today: date | None = None) -> GateNode:
    """产出闸门节点。`knowledge` 由调用方注入，闸门不自行创建仓储。

    degrade/block 分支加入「安全拦截后的确认」中断（T2-32）：不直接返回处方，先中断让
    农户知情确认，恢复输入是枚举 `continue` / `abort`（不是自然语言回答）。恢复后节点
    从头执行，`service.review` 会重跑一次，因此副作用（复核工单）**必须放在中断之后**，
    否则每次恢复都会重复入队一张工单。

    `gate_confirmed` 只作展示与接口层判断，不参与路由——`route_after_gate` 只认
    `decision`：degrade 即便农户选 continue 也仍然转人工（输出的是转人工引导，不是放行
    处方），block 无论选什么都不输出处方。
    """
    service = GateService()

    async def gate_node(state: ConsultationState) -> dict:
        ctx = build_gate_context(state, knowledge=knowledge, today=today or date.today())
        draft = Prescription.model_validate(state["prescription"])
        result = await service.review(draft, ctx)  # 引擎 + 审计 + 影子模式

        # 影子模式下 `result` 是 `passed=True` 的"放行外壳"，真实判定在内层。节点按真实
        # 判定处理输出与路由（T2-45 验收：节点按 shadowed.decision 决定渲染），审计已把
        # 真实判定以 `shadowed=True` 落库（见 T2-46）。
        effective = result.shadowed if result.shadowed is not None else result

        if effective.decision == "allow":
            # 放行：处方卡正常返回，原文保留。
            return {
                "gate_result": dump_contract(effective),
                "stage": ConsultStage.GATE,
                "prescription_text": state.get("prescription_text"),
            }

        # degrade/block：不直接返回处方，先中断让农户知情确认是否继续咨询。
        decision = interrupt(
            {
                "kind": "gate_confirm",
                "decision": effective.decision,
                "why": effective.degrade_output,
                "options": ["continue", "abort"],
            }
        )

        if effective.decision == "degrade":
            # 降级：输出整体替换为 degrade_output，不修改后输出（产品说明书 6.2）。
            return {
                "gate_result": dump_contract(effective),
                "gate_confirmed": decision == "continue",
                "stage": ConsultStage.GATE,
                "prescription_text": effective.degrade_output,
                "prescription_suppressed": True,
            }

        # block：拒绝输出并登记审计事件，同时触发知识条目复核工单。工单落在中断之后，
        # 避免恢复时节点从头执行导致重复入队。
        await _raise_review_ticket(ctx, effective)
        return {
            "gate_result": dump_contract(effective),
            "gate_confirmed": decision == "continue",
            "stage": ConsultStage.BLOCKED,
            "prescription_text": DEGRADE_OUTPUT_TEXT,
            "prescription_suppressed": True,
        }

    # 套追踪（T6-06）：span 名 `gate_check`，由 `traced_node` 统一写 `gate.decision` /
    # `gate.rules_hit`（从返回的 `gate_result` 取）与每节点三属性。闸门不读会话字段，
    # 追踪包装只读节点返回的结构化判定，不触碰 `messages`/`reply`。
    return cast(GateNode, traced_node("gate_check", gate_node))


def build_gate_subgraph(knowledge: KnowledgeFacts | None = None) -> CompiledStateGraph:
    """闸门子图：单节点，入口即出口。挂在主图 prescribe 之后。

    单节点是刻意的：闸门的判定必须原子——要么完成全部规则并给出一个 decision，要么因异常
    走 fail-closed；中间态（"跑了一半规则"）不允许被主图观察到。

    `knowledge` 缺省为 `_EmptyKnowledgeFacts`，因此 `build_gate_subgraph()` 与
    `build_gate_subgraph(knowledge)` 两种调用形态都成立（主图按无参形态挂载）。返回**已编译**
    的子图：主图用 `add_node(name, build_gate_subgraph())` 挂载，LangGraph 只接受 Runnable。
    """
    graph = StateGraph(ConsultationState)
    graph.add_node("gate_check", build_gate_node(knowledge or _EmptyKnowledgeFacts()))
    graph.add_edge(START, "gate_check")
    graph.add_edge("gate_check", END)
    return graph.compile()


__all__ = [
    "build_gate_context",
    "build_gate_node",
    "build_gate_subgraph",
]
