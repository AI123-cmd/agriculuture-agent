"""任务类型路由（T2-57）。

路由是**纯函数式的映射**：不参与调用、不持有状态、不读时间与随机数。同一次问诊内
同一个 `TaskType` 必须解析出相同的档位，这样"某次问诊为什么用了那个模型"可以事后
复算。它把方案 4.4 的模型分层策略从一张表变成可执行的决策。

本模块源码不含任何具体模型名：档位到模型名、供应商链的映射全部由构造时注入的
`TierSpec` 提供，因此开发、预发、生产可以用不同模型而业务代码不动。
"""

from dataclasses import dataclass

from app.llm.enums import TaskType
from app.llm.tiers import DEFAULT_TIER_MAP, ModelTier, TierSpec


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """候选链上的一跳：用哪个供应商的哪个模型。

    `is_degraded` 标记这是链上的降档候选（约定为链尾的私有化模型），让网关与计量
    能标记"这次经过了降档"，与供应商故障导致的降级分开统计——否则无法区分"预算控得
    好"与"服务其实挂了"。
    """

    provider: str
    model: str
    tier: ModelTier
    is_degraded: bool = False


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """一次路由的完整结果：档位、候选链、生成上限、上下文轮数。"""

    task_type: TaskType
    tier: ModelTier
    chain: tuple[ModelCandidate, ...]
    max_tokens: int
    max_history_rounds: int


def _downgrade(tier: ModelTier) -> ModelTier:
    """预算不足时降一档。PRIMARY 降为 MEDIUM，MEDIUM 降为 SMALL，SMALL 不变。

    降档不等同于降级：降档是"用次强模型继续做完"，降级是"放弃模型能力用规则兜底"。
    """
    return {
        ModelTier.PRIMARY: ModelTier.MEDIUM,
        ModelTier.MEDIUM: ModelTier.SMALL,
        ModelTier.VISION: ModelTier.MEDIUM,
    }.get(tier, tier)


class ModelRouter:
    """任务类型路由。纯映射，不持状态。"""

    def __init__(
        self,
        specs: dict[ModelTier, TierSpec],
        *,
        min_budget_for_primary: int,
        tier_map: dict[TaskType, ModelTier] | None = None,
    ) -> None:
        self._specs = specs
        self._min_budget_for_primary = min_budget_for_primary
        self._tier_map = dict(tier_map or DEFAULT_TIER_MAP)
        # 装配期就报错，而不是等某条任务类型第一次被调用时才 KeyError：档位配了
        # 但没给 TierSpec 属配置错误，越早暴露越好。
        configured = {spec.tier for spec in self._specs.values()}
        missing = {tier for tier in self._tier_map.values() if tier not in configured}
        if missing:
            raise ValueError(f"档位 {sorted(t.value for t in missing)} 缺少 TierSpec 配置")

    def resolve(self, task_type: TaskType, *, budget_remaining: int | None = None) -> RoutingDecision:
        """解析出档位与候选链。同一入参永远返回相等结果。"""
        tier = self._tier_map[task_type]
        if budget_remaining is not None and budget_remaining < self._min_budget_for_primary:
            tier = _downgrade(tier)
        spec = self._specs[tier]
        chain = tuple(
            ModelCandidate(
                provider=provider,
                model=spec.model,
                tier=tier,
                is_degraded=(i == len(spec.provider_chain) - 1),
            )
            for i, provider in enumerate(spec.provider_chain)
        )
        return RoutingDecision(
            task_type=task_type,
            tier=tier,
            chain=chain,
            max_tokens=spec.max_tokens,
            max_history_rounds=spec.max_history_rounds,
        )


__all__ = ["DEFAULT_TIER_MAP", "ModelCandidate", "ModelRouter", "RoutingDecision"]
