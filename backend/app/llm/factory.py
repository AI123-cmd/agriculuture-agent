"""网关装配：从环境配置构造 `ModelGateway`（T2-55 的工厂函数）。

网关本身不读环境变量（它是可注入的通用组件，单测要能塞替身），把"配置到对象"的这一步
单独放在这里，业务层只调 `build_gateway()`。档位参数与熔断阈值全部来自 `Settings`：
开发环境用轻量模型跑通链路、生产环境换正式模型，业务代码不动。

真实端点走哪条适配器由**端点能力**决定，而不是由供应商是"云端"还是"私有化"决定：

- `PrivateModelProvider` 走提示词约束路径（不发 `response_format`，把 Schema 说明追加进
  messages），适用于**不支持** `response_format=json_schema` 的 OpenAI 兼容端点。公司网关
  与 DeepSeek 官方均属此类（实测发送 json_schema 返回 502）。
- `OpenAICompatProvider` 走原生结构化输出，仅用于确认支持 `json_schema` 的端点。

`USE_MOCK_LLM=false` 且一个真实端点都没配时仍然**显式报错**而不是静默回落到 Mock——
静默回落会让线上以一个"看起来在跑"的假模型产出农技建议，那是安全事故而不是配置疏漏。
"""

from functools import lru_cache

from app.core.config import get_settings
from app.llm.circuit import CircuitBreaker
from app.llm.gateway import CallAuditor, ModelGateway
from app.llm.metering import NullTokenMeter
from app.llm.providers.base import ModelProvider
from app.llm.providers.mock import MockProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.providers.private_model import PrivateModelProvider
from app.llm.router import ModelRouter
from app.llm.tiers import DEFAULT_TIER_MAP, ModelTier, TierSpec

# 供应商名。`provider_chain` 与 `providers` 的键必须一致，故集中一处。
_MOCK_PROVIDER = "mock"
_PRIVATE_PROVIDER = "private"
_OPENAI_COMPAT_PROVIDER = "openai_compat"


def _provider_chain() -> tuple[dict[str, ModelProvider], tuple[str, ...]]:
    """按配置装配真实适配器，返回 (适配器字典, 候选链)。

    候选链按优先级排列，链尾是降档候选（`ModelCandidate.is_degraded` 据此标记）。
    支持原生结构化输出的端点（`OPENAI_COMPAT_BASE_URL`）放链首；农业大模型端点
    （`FARM_LLM_API_URL`，兼容 `PRIVATE_MODEL_BASE_URL`）走提示词约束路径，放链尾兜底。
    """
    settings = get_settings()
    instances: dict[str, ModelProvider] = {}
    chain: list[str] = []

    if settings.OPENAI_COMPAT_BASE_URL:
        instances[_OPENAI_COMPAT_PROVIDER] = OpenAICompatProvider(
            settings.OPENAI_COMPAT_BASE_URL, settings.OPENAI_COMPAT_API_KEY
        )
        chain.append(_OPENAI_COMPAT_PROVIDER)

    farm_url = settings.FARM_LLM_API_URL or settings.PRIVATE_MODEL_BASE_URL
    farm_key = settings.FARM_API_KEY or settings.PRIVATE_MODEL_API_KEY
    if farm_url:
        instances[_PRIVATE_PROVIDER] = PrivateModelProvider(farm_url, farm_key)
        chain.append(_PRIVATE_PROVIDER)

    if not chain:
        raise RuntimeError(
            "USE_MOCK_LLM=false 但未配置任何真实端点：请设置 FARM_LLM_API_URL / FARM_API_KEY"
        )
    return instances, tuple(chain)


def _tier_specs(provider_chain: tuple[str, ...]) -> dict[ModelTier, TierSpec]:
    """四个档位的规格。

    档位差异体现在模型名与供应商链上。当前只有 VISION 档单独指定模型名（现场采集要打向
    视觉模型），其余档位共用 `LLM_DEFAULT_MODEL`；完整的主模型/降档候选分离属后续任务，
    本阶段只保证"按任务类型取到档位与候选链"这条链路可跑通。
    """
    settings = get_settings()
    models = {
        # 留空即回落默认模型：没配视觉模型不应让现场采集直接失败。
        ModelTier.VISION: settings.LLM_VISION_MODEL or settings.LLM_DEFAULT_MODEL,
    }
    return {
        tier: TierSpec(
            tier=tier,
            model=models.get(tier, settings.LLM_DEFAULT_MODEL),
            provider_chain=provider_chain,
            max_tokens=settings.LLM_MAX_TOKENS,
            max_history_rounds=settings.LLM_MAX_HISTORY_ROUNDS,
        )
        for tier in ModelTier
    }


def build_gateway() -> ModelGateway:
    """按当前环境配置装配网关。"""
    settings = get_settings()
    if settings.USE_MOCK_LLM:
        providers: dict[str, ModelProvider] = {_MOCK_PROVIDER: MockProvider()}
        provider_chain: tuple[str, ...] = (_MOCK_PROVIDER,)
    else:
        providers, provider_chain = _provider_chain()
    router = ModelRouter(
        _tier_specs(provider_chain),
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
        providers=providers,
        circuit=circuit,
        meter=NullTokenMeter(),  # 落库计量由 T2-59 提供
        auditor=CallAuditor(),
    )


@lru_cache(maxsize=1)
def get_gateway() -> ModelGateway:
    """进程内单例：熔断状态与审计记录必须跨调用累积，每次新建会让熔断形同虚设。"""
    return build_gateway()


__all__ = ["build_gateway", "get_gateway"]
