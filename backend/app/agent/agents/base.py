"""智能体基类：契约校验 + 重试 + 降级 + 结构化输出（T2-13）。

把四件横切的事收在一处，每个具体智能体只需声明它的输出契约类型、提示词构造方式、
降级实现与失败行为类别。这四件事决定了安全链路的行为：解析失败必须触发降级而不是
正则硬抽，重试必须有限次且有退避，守卫类智能体必须 fail-closed 而其余可以降级继续
服务。散落在七个智能体里各写一遍，一定会有某一处写漏。

失败行为分两类（全局约定第六节）：

- `DEGRADE`：降级但仍服务。绝大多数智能体属此类。
- `FAIL_CLOSED`：不确定时选更保守的一方。**仅**报告守卫与安全闸门使用。

`AgentContext` 刻意不含数据库会话：L2 规则要求 `app/agent/` 不得直接导入
`app/models/`，智能体要能被喂假数据单测。需要持久化的智能体（如证据核查）通过注入
的仓储接口访问数据，而不是拿 `AsyncSession`。

各智能体的降级出口（技术方案 10.3），子类实现 `degrade` 的依据：

| 智能体 | 降级出口 |
| --- | --- |
| 分诊 | 回退 general 流程，`fallback_reason` 填"模型不可用" |
| 现场采集 | `data_available=False` 并给出 `unavailable_reason` |
| 问诊 | 模板问题（`why` 已在模板中写好） |
| 证据核查 | 返回空集并标注未命中 |
| 判定 | `confidence_level="uncertain"` + 求助引导 |
| 处方 | `requires_tech_station=True`，`pesticide_items=[]` |
| 并行会诊 | 转单链判定 |
| 报告守卫 | `triggered=True`（有疑即报，fail-closed，唯一特例） |
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar, cast

from pydantic import BaseModel
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.agent.agents.exceptions import AgentDegraded, ContractViolation
from app.agent.state import ConsultationState, TaskType
from app.llm.enums import TaskType as GatewayTaskType
from app.llm.errors import TransientModelError
from app.llm.gateway import ModelGateway
from app.observability import log, metrics
from app.process_config.models import ProcessConfig

# 预算低于此值时不再重试。取值依据：一次结构化调用（含系统提示里的 JSON Schema）的
# 典型消耗在千级 token，低于它说明本次问诊的预算已接近尾声，重试只是把剩余预算用在
# 成功率很低的第二次尝试上。
_LOW_BUDGET_TOKENS = 2000

OutT = TypeVar("OutT", bound=BaseModel)


class FailureMode(str, Enum):
    """智能体失败行为。仅守卫类使用 FAIL_CLOSED。"""

    DEGRADE = "degrade"  # 降级但仍服务
    FAIL_CLOSED = "fail_closed"  # 不确定时选更保守的一方


@dataclass(slots=True)
class AgentContext:
    """一次智能体调用的环境依赖。全部为只读视图。"""

    process_config: ProcessConfig
    config_version: int
    task_type: TaskType
    trace_id: str
    region_code: str | None = None
    budget_remaining_tokens: int | None = None


@dataclass(slots=True)
class AgentFailure:
    """一次失败的记录。用于日志、指标与降级原因拼装。

    `reason` 是给人看的（进日志与降级文案），`exc_type` 是给统计用的（按异常类型分组
    才能看出"契约违约多"还是"网关全挂了"）。
    """

    agent: str
    mode: FailureMode
    exc_type: str
    reason: str
    attempts: int = 1


class BaseAgent(Generic[OutT], ABC):
    """智能体基类：契约校验 + 重试 + 降级 + 结构化输出。"""

    name: ClassVar[str]
    output_schema: ClassVar[type[BaseModel]]
    task_type: ClassVar[TaskType]
    failure_mode: ClassVar[FailureMode] = FailureMode.DEGRADE
    # 首次之外的额外尝试次数，总计最多 3 次调用（与技术方案 7.3 的"最多 2 次重试"一致）。
    max_attempts: ClassVar[int] = 2

    def __init__(self, gateway: ModelGateway | None) -> None:
        # 允许为 `None`：报告守卫（T2-22）是纯规则实现，按技术方案 6.5 不调用模型，因此不持有
        # 网关。它覆写 `run` 直接走规则判定，不会到达 `_call_with_retry`；万一被误调到模型路径，
        # `_require_gateway` 会抛契约违约而不是 `AttributeError`，让失败落在明确的分类里。
        self._gateway = gateway

    def _require_gateway(self) -> ModelGateway:
        """取网关，缺网关视为前提缺失（只有不调模型的智能体允许持有 `None`）。

        抛 `AgentDegraded` 而不是 `ContractViolation`：后者是可重试类，而"这个智能体本就
        不该调模型"重试一百次也不会改变结果，只会把问诊延迟拉长。
        """
        if self._gateway is None:
            raise AgentDegraded(f"{self.name} 未持有模型网关，不应进入模型调用路径")
        return self._gateway

    # ---- 子类必须实现 ----

    @abstractmethod
    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词。结构化字段注入，不拼接自由文本。"""

    @abstractmethod
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> OutT:
        """降级路径。必须返回满足契约的对象，不得返回 None。"""

    # ---- 子类可选覆盖 ----

    async def post_validate(self, result: OutT, state: ConsultationState, ctx: AgentContext) -> OutT:
        """契约之外的业务校验与字段覆写，如判定契约的排除项非空检查。"""
        return result

    def validate_output(self, result: OutT, state: ConsultationState, ctx: AgentContext) -> None:
        """可覆盖的额外校验，抛 `ContractViolation` 触发重试。"""
        return None

    async def fail_closed(self, state: ConsultationState, ctx: AgentContext, reason: str) -> OutT:
        """守卫类失败路径。

        默认实现抛异常而不是给一个"保守值"：不同守卫的"保守"含义相反（报告守卫的保守
        是"报"，安全闸门的保守是"拦"），基类无法给出通用值。强制子类显式声明。
        """
        raise NotImplementedError(f"{self.name} 声明 fail_closed 但未实现 fail_closed 路径")

    # ---- 统一入口 ----

    async def run(self, state: ConsultationState, ctx: AgentContext) -> OutT:
        """统一入口：调用、校验、降级。任何异常都不会逸出到调用方。"""
        try:
            result = await self._call_with_retry(state, ctx)
            result = await self.post_validate(result, state, ctx)
            log.info("agent.ok", agent=self.name, trace_id=ctx.trace_id)
            return result
        except Exception as exc:
            # 宽泛捕获是刻意的：调用方（图节点）不该因为某个智能体的意外异常而整条链路
            # 崩掉，降级出口就是为这种情况准备的。
            return await self._on_failure(state, ctx, exc)

    def effective_max_attempts(self, ctx: AgentContext) -> int:
        """预算紧张时的重试次数。`ctx.budget_remaining_tokens=None` 表示不限制。"""
        if ctx.budget_remaining_tokens is None:
            return self.max_attempts
        return 1 if ctx.budget_remaining_tokens < _LOW_BUDGET_TOKENS else self.max_attempts

    async def _call_with_retry(self, state: ConsultationState, ctx: AgentContext) -> OutT:
        """带指数退避的重试。仅对契约违约与瞬时故障重试。"""
        attempt_state: dict[str, Any] = {"n": 0, "last_error": None}
        # max_attempts 是"首次之外的额外次数"，因此总尝试次数为 max_attempts + 1。
        total_attempts = self.effective_max_attempts(ctx) + 1

        @retry(
            stop=stop_after_attempt(total_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type((ContractViolation, TransientModelError)),
            reraise=True,
            before_sleep=self._log_retry,
        )
        async def _attempt() -> OutT:
            attempt_state["n"] += 1
            messages = self.build_messages(state, ctx)
            if attempt_state["n"] > 1:
                messages = self._append_repair_hint(messages, attempt_state["last_error"])
            response = await self._require_gateway().chat(
                cast(GatewayTaskType, ctx.task_type), messages, self.output_schema
            )
            if response.parsed is None:
                attempt_state["last_error"] = response.error or "解析失败"
                raise ContractViolation(f"{self.name} 结构化输出解析失败")
            result = self.output_schema.model_validate(response.parsed)
            self.validate_output(cast(OutT, result), state, ctx)
            return cast(OutT, result)

        return await _attempt()

    def _append_repair_hint(self, messages: list[dict], last_error: str | None) -> list[dict]:
        """把上一次的校验错误反馈给模型，让它自己修。不做正则硬抽。

        技术方案 10.2 明确"为什么不做解析失败就用正则硬抽"：农业场景下错误的字段值比
        没有字段值更危险（编造的剂量数字会被农户照着用）。唯一补救手段是让模型重出，
        重出失败就走降级。
        """
        if not last_error:
            return messages
        return [
            *messages,
            {"role": "assistant", "content": "（上一次输出未通过校验）"},
            {
                "role": "user",
                "content": f"你的上一次输出不符合要求：{last_error}。请仅输出符合 JSON Schema 的修正结果。",
            },
        ]

    def _log_retry(self, retry_state: RetryCallState) -> None:
        log.warning(
            "agent.retry",
            agent=self.name,
            attempt=retry_state.attempt_number,
            error=str(retry_state.outcome.exception()) if retry_state.outcome else None,
        )

    async def _on_failure(self, state: ConsultationState, ctx: AgentContext, exc: Exception) -> OutT:
        """失败分派：指标上报一次，然后按 `failure_mode` 走降级或 fail-closed。"""
        failure = AgentFailure(
            agent=self.name,
            mode=self.failure_mode,
            exc_type=type(exc).__name__,
            reason=f"{type(exc).__name__}: {exc}",
        )
        log.warning(
            "agent.degraded",
            agent=failure.agent,
            failure_mode=failure.mode.value,
            reason=failure.reason,
        )
        metrics.incr("agent_degraded_total", tags={"agent": self.name, "mode": self.failure_mode.value})
        if self.failure_mode is FailureMode.FAIL_CLOSED:
            result = await self.fail_closed(state, ctx, failure.reason)
        else:
            result = await self.degrade(state, ctx, failure.reason)
        # 降级产出同样要过一遍业务校验：降级路径写错字段（如漏了 `unavailable_reason`）
        # 必须在这里暴露，而不是等到下游消费时才以 KeyError 的形式出现。
        return await self.post_validate(result, state, ctx)

    def _schema_for_prompt(self) -> str:
        """把契约的 JSON Schema 注入系统提示。与网关的原生结构化输出形成双保险。"""
        return json.dumps(self.output_schema.model_json_schema(), ensure_ascii=False)


__all__ = ["AgentContext", "AgentFailure", "BaseAgent", "FailureMode"]
