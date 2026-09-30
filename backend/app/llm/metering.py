"""token 计量与成本估算（T2-59，技术方案 10.1 / 10.4）。

网关在候选链上的每一次**成功**调用后回写一条 `llm_call`；候选链全部失败时只补一条
"最终失败"记录。中间候选的失败不写：失败响应没有可信的 token 数，为每次尝试都写一条
会污染成本统计（失败次数由熔断器承担，与计量分开）。

**独立事务**：计量是旁路数据，不应因为业务事务回滚而丢失，也不应因为计量写失败而回滚
业务。因此本模块自己开会话提交，并在写失败时只记日志、不向上抛——模型调用的返回不能
被计量拖垮。

**成本是纯计算**：由 token 数与单价相乘得到，不涉及模型推断，也不参与任何安全判断。
`cost_micros` 用整数存微元（1e-6 元）避免浮点累计误差；未配置单价的模型记录 token 但
成本记 0，而不是报错——单价更新滞后于模型上线是常态。

单价表按"每千 token 的微元"配置，输入与输出分开，因为多数供应商的输出单价远高于输入。
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.providers.base import ModelResponse
from app.llm.router import ModelCandidate

logger = logging.getLogger(__name__)

# 每千 token 的微元除数：单价以"每千 token"为单位，token 数先乘单价再整除 1000。
_TOKENS_PER_UNIT = 1000


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """一个模型的单价：每千 token 的微元（1e-6 元），输入与输出分开。"""

    input_micros_per_k: int
    output_micros_per_k: int


def pricing_from_settings(raw: Mapping[str, Any]) -> dict[str, ModelPricing]:
    """把配置里的 `{模型名: [输入单价, 输出单价]}` 转成 `ModelPricing` 表。

    非法条目（长度不为 2、非整数）直接跳过而非报错：单价是统计口径的输入，缺一条
    只影响该模型成本记 0，不应让整个进程起不来。
    """
    pricing: dict[str, ModelPricing] = {}
    for model, pair in raw.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            logger.warning("单价配置格式非法，已忽略：model=%s pair=%r", model, pair)
            continue
        try:
            pricing[model] = ModelPricing(int(pair[0]), int(pair[1]))
        except (TypeError, ValueError):
            logger.warning("单价配置无法转整数，已忽略：model=%s pair=%r", model, pair)
    return pricing


@dataclass(frozen=True, slots=True)
class CallContext:
    """一次调用的业务上下文。带上它计量才能按流程、按智能体、按用户归属。

    网关是通用组件、不知道自己在为哪个智能体服务，这些字段由调用方（智能体基类或
    服务层）注入。缺省时 `agent` 退化为任务类型名，保证 `agent` 这一非空列总有值。
    """

    agent: str
    user_id: str | None = None
    consultation_id: str | None = None
    process_id: str | None = None
    model_version: str | None = None
    input_digest: str | None = None


class TokenMeter(Protocol):
    """计量接口。网关只依赖这一个方法。"""

    async def record(
        self,
        task_type: Any,
        response: ModelResponse,
        candidate: ModelCandidate,
        *,
        is_fallback: bool,
        is_degraded: bool,
        ctx: CallContext | None = None,
        input_digest: str | None = None,
    ) -> None: ...


class NullTokenMeter:
    """不落库的计量实现。用于无数据库的单元测试与未装配计量的场景。"""

    async def record(
        self,
        task_type: Any,
        response: ModelResponse,
        candidate: ModelCandidate,
        *,
        is_fallback: bool,
        is_degraded: bool,
        ctx: CallContext | None = None,
        input_digest: str | None = None,
    ) -> None:
        return None


class TokenMeterImpl:
    """落库计量与成本估算。

    构造接收会话工厂而非会话：独立事务要求本类自己开关会话，若由调用方传入会话，
    写入会顺带加入调用方的事务，"计量不随业务回滚"这条前提就不成立。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pricing: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._pricing = dict(pricing or {})

    def estimate_cost_micros(self, model: str, prompt_tokens: int, completion_tokens: int) -> int:
        """按单价估算成本（微元）。未配置单价的模型返回 0。"""
        price = self._pricing.get(model)
        if price is None:
            return 0
        return (
            prompt_tokens * price.input_micros_per_k // _TOKENS_PER_UNIT
            + completion_tokens * price.output_micros_per_k // _TOKENS_PER_UNIT
        )

    async def record(
        self,
        task_type: Any,
        response: ModelResponse,
        candidate: ModelCandidate,
        *,
        is_fallback: bool,
        is_degraded: bool,
        ctx: CallContext | None = None,
        input_digest: str | None = None,
    ) -> None:
        """写一条调用记录。写失败只记日志，不阻断模型调用返回。"""
        # 延迟导入：本模块属 `app.llm`（L3 约束），导入 ORM 模型会触碰 `app.models`；
        # 放在方法内导入使"网关不依赖 ORM"在模块加载层面依然成立，也避免包初始化期的
        # 循环导入。
        from app.models.llm_call import LlmCall

        model = response.model or candidate.model
        row = LlmCall(
            user_id=ctx.user_id if ctx else None,
            consultation_id=ctx.consultation_id if ctx else None,
            process_id=ctx.process_id if ctx else None,
            agent=ctx.agent if ctx else str(getattr(task_type, "value", task_type)),
            task_type=str(getattr(task_type, "value", task_type)),
            tier=str(getattr(candidate.tier, "value", candidate.tier)),
            provider=candidate.provider,
            model=model,
            model_version=ctx.model_version if ctx else None,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost_micros=self.estimate_cost_micros(model, response.prompt_tokens, response.completion_tokens),
            latency_ms=response.latency_ms,
            is_fallback=is_fallback,
            is_degraded=is_degraded,
            ok=response.error is None,
            error_code=response.error,
            input_digest=input_digest if input_digest is not None else (ctx.input_digest if ctx else None),
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001  计量是旁路数据，任何写失败都不应影响模型调用
            logger.exception("llm_call 计量写入失败，已忽略：task_type=%s provider=%s", task_type, candidate.provider)


__all__ = [
    "CallContext",
    "ModelPricing",
    "NullTokenMeter",
    "TokenMeter",
    "TokenMeterImpl",
    "pricing_from_settings",
]
