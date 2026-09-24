"""网关装配：从环境配置构造 `ModelGateway`（T2-55 的工厂函数）。

网关本身不读环境变量（它是可注入的通用组件，单测要能塞替身），把"配置到对象"的这一步
单独放在这里，业务层只调 `build_gateway()`。档位参数与熔断阈值全部来自 `Settings`：
开发环境用轻量模型跑通链路、生产环境换正式模型，业务代码不动。

刻意的取舍：真实供应商适配器由 T2-56 提供，目前只有 `MockProvider`。因此
`USE_MOCK_LLM=false` 时本函数**显式报错**而不是静默回落到 Mock——静默回落会让线上以一个
"看起来在跑"的假模型产出农技建议，那是安全事故而不是配置疏漏。
"""

from functools import lru_cache

from app.core.config import get_settings
from app.llm.circuit import CircuitBreaker
from app.llm.gateway import CallAuditor, ModelGateway
from app.llm.metering import NullTokenMeter
from app.llm.providers.mock import MockProvider
from app.llm.router import ModelRouter
from app.llm.tiers import DEFAULT_TIER_MAP, ModelTier, TierSpec

# Mock 供应商名。`provider_chain` 与 `providers` 的键必须一致，故集中一处。
_MOCK_PROVIDER = "mock"


def _tier_specs() -> dict[ModelTier, TierSpec]:
    """四个档位的规格。

    档位差异体现在模型名与供应商链上，当前两者都由配置提供同一个模型名：真实的档位到模型
    映射（主模型与私有化降档候选）属 T2-56 的适配器范围，本阶段只保证"按任务类型取到档位
    与候选链"这条链路可跑通。
    """
    settings = get_settings()
    return {
        tier: TierSpec(
            tier=tier,
            model=settings.LLM_DEFAULT_MODEL,
            provider_chain=(_MOCK_PROVIDER,),
            max_tokens=settings.LLM_MAX_TOKENS,
            max_history_rounds=settings.LLM_MAX_HISTORY_ROUNDS,
        )
        for tier in ModelTier
    }


def build_gateway() -> ModelGateway:
    """按当前环境配置装配网关。"""
    settings = get_settings()
    if not settings.USE_MOCK_LLM:
        raise RuntimeError("USE_MOCK_LLM=false 需要 T2-56 的真实供应商适配器，当前尚未装配")
    router = ModelRouter(
        _tier_specs(),
        min_budget_for_primary=settings.LLM_MIN_BUDGET_FOR_PRIMARY,
        tier_map=DEFAULT_TIER_MAP,
    )
    circuit = CircuitBreaker(
        failure_threshold=settings.CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=settings.CIRCUIT_COOLDOWN_SECONDS,
        half_open_probes=settings.CIRCUIT_HALF_OPEN_PROBES,
    )
    return ModelGateway(
        router=router,
        providers={_MOCK_PROVIDER: MockProvider()},
        circuit=circuit,
        meter=NullTokenMeter(),  # 落库计量由 T2-59 提供
        auditor=CallAuditor(),
    )


@lru_cache(maxsize=1)
def get_gateway() -> ModelGateway:
    """进程内单例：熔断状态与审计记录必须跨调用累积，每次新建会让熔断形同虚设。"""
    return build_gateway()


__all__ = ["build_gateway", "get_gateway"]
