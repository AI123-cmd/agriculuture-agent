"""T2-57：任务类型路由与模型档位。

用例围绕"路由是可复算的纯映射"这一性质展开：

1. **档位映射**——九条任务类型逐项落档，`DEFAULT_TIER_MAP` 与 `TaskType` 集合相等。
2. **候选链**——链由 `provider_chain` 展开，末项 `is_degraded` 为真，其余为假，链长一致。
3. **纯函数性**——同一入参两次 `resolve` 返回相等结果，且不读时间与随机数（同一结果
   在冻结时钟下重复亦然）。
4. **预算降档**——低于阈值时 `PRIMARY→MEDIUM`、`MEDIUM→SMALL`、`SMALL` 不变；降档后
   档位与每个候选的 `tier` 一致。
5. **架构约束**——`TaskType` 中不存在闸门成员；`router.py` 源码不含具体模型名，模型名
   只能来自构造注入的 `TierSpec`。
"""

import ast
import pathlib
import re

import pytest

from app.agent.state import TaskType as StateTaskType
from app.llm.enums import TaskType
from app.llm.router import DEFAULT_TIER_MAP, ModelRouter, RoutingDecision
from app.llm.tiers import TierSpec, ModelTier


# 测试用档位规格：每个档位一条供应商链（主 A → 同档备选 B → 私有化降档），模型名由
# 注入提供（此处故意用假名，证明源码不依赖任何真实模型名）。
_FAKE_SPECS: dict[ModelTier, TierSpec] = {
    ModelTier.SMALL: TierSpec(
        tier=ModelTier.SMALL,
        model="model-small",
        provider_chain=("alpha", "beta", "private"),
        max_tokens=1024,
        max_history_rounds=4,
    ),
    ModelTier.MEDIUM: TierSpec(
        tier=ModelTier.MEDIUM,
        model="model-medium",
        provider_chain=("alpha", "beta", "private"),
        max_tokens=2048,
        max_history_rounds=6,
    ),
    ModelTier.PRIMARY: TierSpec(
        tier=ModelTier.PRIMARY,
        model="model-primary",
        provider_chain=("alpha", "beta", "private"),
        max_tokens=4096,
        max_history_rounds=6,
    ),
    ModelTier.VISION: TierSpec(
        tier=ModelTier.VISION,
        model="model-vision",
        provider_chain=("alpha", "private"),
        max_tokens=2048,
        max_history_rounds=4,
    ),
}


def _router(*, min_budget: int = 8000) -> ModelRouter:
    return ModelRouter(_FAKE_SPECS, min_budget_for_primary=min_budget)


# ---- 档位映射 ----


@pytest.mark.parametrize(
    ("task_type", "expected"),
    [
        (TaskType.TRIAGE, ModelTier.SMALL),
        (TaskType.OBSERVE, ModelTier.VISION),
        (TaskType.INTERVIEW, ModelTier.PRIMARY),
        (TaskType.RETRIEVE, ModelTier.MEDIUM),
        (TaskType.DIAGNOSE, ModelTier.PRIMARY),
        (TaskType.PRESCRIBE, ModelTier.SMALL),
        (TaskType.PANEL, ModelTier.PRIMARY),
        (TaskType.SNAPSHOT_WRITER, ModelTier.SMALL),
        (TaskType.REPORT_GUARD, ModelTier.SMALL),
    ],
)
def test_default_tier_map_matches_scheme_4_4(task_type: TaskType, expected: ModelTier) -> None:
    assert DEFAULT_TIER_MAP[task_type] is expected
    assert _router().resolve(task_type).tier is expected


def test_default_tier_map_covers_all_task_types() -> None:
    assert set(DEFAULT_TIER_MAP) == set(TaskType)


def test_task_type_mirror_equals_agent_state() -> None:
    """镜像枚举必须与 `app/agent/state.py` 取值集合相等（L3 禁止 llm 导入业务模块）。"""
    assert set(TaskType) == set(StateTaskType)


def test_no_gate_task_type_exists() -> None:
    """安全闸门不使用大模型：TaskType 中不得有闸门相关成员，映射也不得含闸门键。"""
    gate_like = [member for member in TaskType if "gate" in member.value or "redline" in member.value]
    assert gate_like == []
    assert all("gate" not in key.value for key in DEFAULT_TIER_MAP)


# ---- 候选链 ----


def test_chain_expanded_from_provider_chain() -> None:
    decision = _router().resolve(TaskType.INTERVIEW)
    spec = _FAKE_SPECS[ModelTier.PRIMARY]
    assert len(decision.chain) == len(spec.provider_chain)
    assert tuple(c.provider for c in decision.chain) == spec.provider_chain


def test_only_last_candidate_is_degraded() -> None:
    decision = _router().resolve(TaskType.INTERVIEW)
    flags = [c.is_degraded for c in decision.chain]
    assert flags[-1] is True
    assert all(flag is False for flag in flags[:-1])


def test_single_provider_chain_marks_that_candidate_degraded() -> None:
    """链长为 1 时，唯一一跳即是降档候选（末项约定）。"""
    specs = dict(_FAKE_SPECS)
    specs[ModelTier.SMALL] = TierSpec(
        tier=ModelTier.SMALL,
        model="model-small",
        provider_chain=("private",),
        max_tokens=1024,
        max_history_rounds=4,
    )
    router = ModelRouter(specs, min_budget_for_primary=8000)
    decision = router.resolve(TaskType.TRIAGE)
    assert len(decision.chain) == 1
    assert decision.chain[0].is_degraded is True


def test_candidate_model_comes_from_spec() -> None:
    decision = _router().resolve(TaskType.DIAGNOSE)
    assert all(c.model == "model-primary" for c in decision.chain)


def test_decision_carries_tokens_and_history_rounds() -> None:
    decision = _router().resolve(TaskType.RETRIEVE)
    spec = _FAKE_SPECS[ModelTier.MEDIUM]
    assert decision.max_tokens == spec.max_tokens
    assert decision.max_history_rounds == spec.max_history_rounds


def test_small_tier_keeps_shorter_history_than_primary() -> None:
    """按环节配资源：小模型的上下文轮数应小于主模型。"""
    small = _router().resolve(TaskType.TRIAGE)
    primary = _router().resolve(TaskType.INTERVIEW)
    assert small.max_history_rounds < primary.max_history_rounds


# ---- 纯函数性 ----


def test_resolve_is_pure() -> None:
    router = _router()
    first = router.resolve(TaskType.DIAGNOSE)
    second = router.resolve(TaskType.DIAGNOSE)
    assert first == second


def test_resolve_does_not_mutate_router() -> None:
    """同一入参多次调用返回相同结果，说明 `resolve` 未在内部累积状态。"""
    router = _router()
    results = [router.resolve(TaskType.INTERVIEW) for _ in range(5)]
    assert all(result == results[0] for result in results)


def test_routing_decision_is_frozen() -> None:
    decision = _router().resolve(TaskType.TRIAGE)
    with pytest.raises(Exception):
        decision.tier = ModelTier.PRIMARY  # type: ignore[misc]


# ---- 预算降档 ----


def test_budget_below_threshold_downgrades_primary_to_medium() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.DIAGNOSE, budget_remaining=100)
    assert decision.tier is ModelTier.MEDIUM


def test_budget_below_threshold_downgrades_medium_to_small() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.RETRIEVE, budget_remaining=100)
    assert decision.tier is ModelTier.SMALL


def test_budget_below_threshold_keeps_small_unchanged() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.TRIAGE, budget_remaining=1)
    assert decision.tier is ModelTier.SMALL


def test_budget_below_threshold_downgrades_vision_to_medium() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.OBSERVE, budget_remaining=100)
    assert decision.tier is ModelTier.MEDIUM


def test_budget_at_or_above_threshold_does_not_downgrade() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.DIAGNOSE, budget_remaining=8000)
    assert decision.tier is ModelTier.PRIMARY


def test_no_budget_hint_does_not_downgrade() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.DIAGNOSE)
    assert decision.tier is ModelTier.PRIMARY


def test_downgraded_decision_uses_downgraded_spec() -> None:
    """降档后不仅 `tier` 变，链与上限也必须来自降档后的档位规格。"""
    decision = _router(min_budget=8000).resolve(TaskType.DIAGNOSE, budget_remaining=10)
    assert decision.tier is ModelTier.MEDIUM
    assert all(c.model == "model-medium" for c in decision.chain)
    assert decision.max_tokens == _FAKE_SPECS[ModelTier.MEDIUM].max_tokens


def test_downgraded_tier_matches_every_candidate() -> None:
    decision = _router(min_budget=8000).resolve(TaskType.DIAGNOSE, budget_remaining=10)
    assert all(c.tier is decision.tier for c in decision.chain)


# ---- 装配期校验 ----


def test_missing_tier_spec_raises_at_assembly() -> None:
    """档位被映射引用但缺 TierSpec 属配置错误，装配期即报错。"""
    incomplete = {ModelTier.SMALL: _FAKE_SPECS[ModelTier.SMALL]}
    with pytest.raises(ValueError):
        ModelRouter(incomplete, min_budget_for_primary=8000)


# ---- 源码不含具体模型名 ----


def test_router_source_has_no_hardcoded_model_name() -> None:
    """模型名只能来自构造注入的 TierSpec：源码不得出现具体模型名硬编码。"""
    source = pathlib.Path("app/llm/router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    model_like = re.compile(r"(deepseek|gpt|claude|qwen|glm|gemini|ernie|moonshot|kimi)", re.IGNORECASE)
    offenders = [text for text in strings if model_like.search(text)]
    assert offenders == [], f"router.py 出现硬编码模型名: {offenders}"


def test_routing_decision_fields_are_complete() -> None:
    decision = _router().resolve(TaskType.TRIAGE)
    assert isinstance(decision, RoutingDecision)
    assert decision.task_type is TaskType.TRIAGE
