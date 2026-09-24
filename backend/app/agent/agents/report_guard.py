"""报告守卫：法定报告义务的识别与升级（T2-22）。

守卫检测识别信号，命中即中止问诊、给出强制动作与本地联系入口。它是仅有的两个 fail-closed
组件之一（另一个是安全闸门），失败行为是"有疑即报"。

**本项要纠正的认知盲区**：不是只有群死群伤才需报告，个别猪只出现可疑症状同样触发报告义务。
因此 `trigger_source="signal"` 的判定条件**必须极低**——单个识别信号命中即触发。任何"为了
降低误报率而给信号数量加门槛"的改动都是产品决策而非工程决策，必须显式提出并经过确认。

三处刻意不用模型：

- **信号匹配**：`match_signals` 是纯字符串包含（见 `report_signals.py`）。模型认为"这一例不
  严重"不能阻止信号命中后的触发。
- **强制动作**：按 `trigger_source` 等级从固定文案表取，模型无权改写。文案里不得出现任何
  疾病名称或"确诊"字样：技术方案 6.7 明确养猪流程的疫情报告"不做疫情的最终认定"。
- **联系入口**：由注入的 `ContactDirectory` 解析；解析不到时留空并记 ERROR，**绝不编造号码**
  ——编造的联系方式会把农户引向无效渠道，比不提供更糟。

## 本类的特例：不持有模型网关

`ReportGuardAgent.__init__` 不接收网关，`super().__init__(gateway=None)` 是刻意的，不是遗漏。
`run` 被覆写为直接调用 `collect_decision`，不走基类的"调模型出契约 + 指数退避重试"路径：
技术方案 5.1 把报告守卫的自治程度标为"极低（规则优先）"，6.5 的独立性表明确要求不调用模型。
`degrade` 因此在本类里无正常入口，实现为委托 `fail_closed`，保证万一被调用也落在保守一侧。
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.agents.report_signals import match_signals, resolve_trigger_source
from app.agent.contracts import ContactInfo, ObservationResult, ReportDecision, dump_contract
from app.agent.state import ConsultationState, ProcessId, TaskType
from app.observability import log
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig

# 守卫节点的形态：只吃 state，返回 state 增量。不带 `config` 参数是刻意的——LangGraph 会把
# 运行时 `RunnableConfig` 按形参名注入，而流程配置在节点构造时就已绑定，两者不应混在一个参数上。
ReportGuardNode = Callable[[ConsultationState], Awaitable[dict]]

# 三条基础动作对所有触发等级都成立：先隔离、再消毒、不自行处置病死猪。
# "不要自行处理或出售病死猪只"是法定义务提示（`pig.yaml` 的 PIG-03 红线同源）。
_BASE_ACTIONS: tuple[str, ...] = (
    "立即把发病猪只与健康猪只分开，单独饲喂与管理",
    "对栏舍、用具与通道进行消毒，人员进出更换衣物鞋具",
    "不要自行处理或出售病死猪只",
)

# 群组与群体死亡情形：范围更大，动作相应增加流动管控与头数说明。
_GROUP_ACTIONS: tuple[str, ...] = (
    "暂停猪只与人员的场内外流动",
    "尽快联系当地畜牧兽医部门，说明发病头数与时间",
)

# signal 级（个别发病）的最后一条刻意写明"个别发病也需要报告"。这条文案本身承担纠正认知
# 盲区的作用：农户看到的不只是"要报告"，还有"我这种情况也要报"，从而不会因为"只有一头"
# 而自行判断无需报告。
_SIGNAL_ACTIONS: tuple[str, ...] = ("尽快联系当地畜牧兽医部门报告，个别发病也需要报告",)

# 守卫自身异常时的强制动作。比常规触发更保守：先暂停移动与售卖。
_FAILURE_ACTIONS: tuple[str, ...] = (
    "暂停猪只移动与售卖",
    "尽快联系当地畜牧兽医部门说明情况",
)

_FAILURE_SIGNAL_ID = "guard_failure"


class ContactDirectory(Protocol):
    """本地联系入口的只读接口。T4-04 提供实现，本阶段由假实现注入。

    返回空列表是合法结果：地图与地址尚未接入时，守卫据此返回一条 `phone=None` 的条目并
    记 ERROR 日志，而不是编造号码。
    """

    def for_region(self, region_code: str | None) -> list[ContactInfo]:
        """按行政区编码取本地联系入口。解析不到返回空列表。"""


class _EmptyDirectory:
    """未接入联系方式的占位实现：返回空列表，由 `_resolve_contacts` 记 ERROR 并给出口径。"""

    def for_region(self, region_code: str | None) -> list[ContactInfo]:
        return []


class ReportGuardAgent(BaseAgent[ReportDecision]):
    """报告守卫：规则匹配、fail-closed、零模型调用。"""

    name = "report_guard"
    output_schema = ReportDecision
    task_type = TaskType.REPORT_GUARD
    failure_mode = FailureMode.FAIL_CLOSED  # 有疑即报
    # 守卫是规则实现，无重试语义。该声明只用于表达意图：本类的 `run` 不走基类的重试入口。
    max_attempts = 1

    def __init__(self, contacts: ContactDirectory) -> None:
        super().__init__(gateway=None)  # 守卫不持有模型网关，见模块 docstring
        self._contacts = contacts

    # ---- 统一入口（覆写：无模型调用，无重试） ----

    async def run(self, state: ConsultationState, ctx: AgentContext) -> ReportDecision:
        """主流程：规则匹配取决定，失败走 fail-closed。

        不走基类的"调模型出契约"入口：本智能体的产出全部是规则结论，模型无权参与，让模型
        先出一版结论再由代码整份覆盖只会多一次调用与一份可能被误用的中间结果。降级与指标
        仍复用基类的失败分派（`_on_failure`），行为与其余智能体一致。
        """
        try:
            decision = self.collect_decision(state, ctx)
            return await self.post_validate(decision, state, ctx)
        except Exception as exc:
            # 宽泛捕获是刻意的：调用方（图节点）不该因为守卫自身的意外异常而整条链路崩掉，
            # 而守卫的出口必须落在保守一侧——`_on_failure` 会按 FAIL_CLOSED 走 `fail_closed`。
            return await self._on_failure(state, ctx, exc)

    # ---- 核心决策 ----

    def collect_decision(self, state: ConsultationState, ctx: AgentContext) -> ReportDecision:
        """核心决策：规则匹配，无模型参与。

        `observations` 为 `ObservationResult` 的序列化字典，读出时走完整校验；上游尚未采集
        现场数据时为 `None`，此时只有槽位规则与文本规则参与匹配。
        """
        raw = state.get("observations")
        observations = ObservationResult.model_validate(raw) if raw else None
        hits = match_signals(state, observations)
        source = resolve_trigger_source(hits)
        if not hits:
            return ReportDecision(
                triggered=False,
                matched_signals=[],
                trigger_source="none",
                mandatory_actions=[],
                contacts=[],
                stop_consultation=False,
            )
        return ReportDecision(
            triggered=True,
            matched_signals=hits,
            trigger_source=source,
            mandatory_actions=self.mandatory_actions(source),
            contacts=self.resolve_contacts(ctx),
            stop_consultation=True,
        )

    # ---- 强制动作 ----

    def mandatory_actions(self, source: str) -> list[str]:
        """按触发等级给出强制动作。文案固定，模型无权改写，顺序即展示顺序。

        `signal` 分支的最后一条刻意写明"个别发病也需要报告"；文案中不得出现任何疾病名称或
        "确诊"字样（技术方案 6.7：不做疫情的最终认定）。
        """
        actions = list(_BASE_ACTIONS)
        if source in ("group", "mass_death"):
            actions.extend(_GROUP_ACTIONS)
        else:
            actions.extend(_SIGNAL_ACTIONS)
        return actions

    # ---- 联系入口 ----

    def resolve_contacts(self, ctx: AgentContext) -> list[ContactInfo]:
        """按地区解析联系入口。解析不到时必须留空并告警，不编造号码。"""
        try:
            contacts = list(self._contacts.for_region(ctx.region_code))
        except Exception as exc:
            log.error(
                "report contacts lookup failed",
                extra={"region_code": ctx.region_code, "trace_id": ctx.trace_id, "reason": str(exc)},
            )
            contacts = []
        if not contacts:
            log.error(
                "report contacts unresolved",
                extra={"region_code": ctx.region_code, "trace_id": ctx.trace_id},
            )
            return [
                ContactInfo(
                    kind="hotline",
                    name="当地畜牧兽医部门",
                    phone=None,
                    region_code=ctx.region_code,
                    note="请通过当地政府或农业农村部门查询联系电话",
                )
            ]
        return contacts

    # ---- fail-closed 与基类契约 ----

    async def fail_closed(self, state: ConsultationState, ctx: AgentContext, reason: str) -> ReportDecision:
        """守卫失败路径：有疑即报。宁可多报一次，不可漏报一次。

        `matched_signals` 填 `guard_failure` 而不是伪造一个具体症状名：让审计能区分"因规则
        命中而报"与"因守卫异常而报"。后者占比升高说明守卫实现有问题，需要修复而不是调信号表。
        """
        log.error("report guard failed, defaulting to report", extra={"reason": reason, "trace_id": ctx.trace_id})
        return ReportDecision(
            triggered=True,
            matched_signals=[_FAILURE_SIGNAL_ID],
            trigger_source="signal",
            mandatory_actions=list(_FAILURE_ACTIONS),
            contacts=self.resolve_contacts(ctx),
            stop_consultation=True,
        )

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> ReportDecision:
        """本类不走降级路径（`failure_mode` 为 FAIL_CLOSED），委托给 fail-closed 以落在保守一侧。

        保留实现而非抛异常：万一有调用方绕过 `failure_mode` 直接调用，得到的仍应是"报"，
        而不是一个会打断整条链路的异常。
        """
        log.warning("report guard degrade path called, falling back to fail-closed", reason=reason)
        return await self.fail_closed(state, ctx, reason)

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """基类契约要求的提示词构造。本智能体零模型调用，故返回空列表。"""
        return []


def build_report_guard_node(
    config: ProcessConfig | None = None,
    *,
    contacts: ContactDirectory | None = None,
) -> ReportGuardNode:
    """构造报告守卫节点（主图与养猪子图共用）。

    `config=None` 时节点按 state 上的 `process_id` 取流程配置（T2-25 的主图用法）；
    养猪子图传入固定的 `ProcessId.PIG` 配置（T2-28 的用法），两者共用同一个节点函数。

    节点只写 `report_decision`，**不改 `stage`**：子图内 `route_after_subgraph_guard` 靠
    `stage` 区分"入口守卫"与"observe 之后的兜底守卫"，节点改 stage 会让这个判据失真。
    """
    agent = ReportGuardAgent(contacts if contacts is not None else _EmptyDirectory())

    async def report_guard_node(state: ConsultationState) -> dict:
        process_config = config if config is not None else _config_for(state)
        ctx = AgentContext(
            process_config=process_config,
            config_version=state.get("config_version") or process_config.version,
            task_type=TaskType.REPORT_GUARD,
            trace_id=state.get("trace_id") or "",
            region_code=state.get("region_code"),
        )
        decision = await agent.run(state, ctx)
        return {"report_decision": dump_contract(decision)}

    return report_guard_node


def _config_for(state: ConsultationState) -> ProcessConfig:
    """按 state 上的 `process_id` 取流程配置；缺失时回落到通用流程。

    回落到 general 而不是抛异常：守卫的职责是"不漏报"，因为一份配置读不到就不做检查，等于
    把漏报风险放在配置错误之后。通用流程的配置足以支撑 `AgentContext` 的构造。
    """
    raw = state.get("process_id")
    if isinstance(raw, ProcessId):
        return get_process_config(raw)
    try:
        return get_process_config(ProcessId(str(raw)))
    except ValueError:
        return get_process_config(ProcessId.GENERAL)


__all__ = [
    "ContactDirectory",
    "ReportGuardAgent",
    "build_report_guard_node",
]
