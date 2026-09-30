"""紧急判定的双条件规格（T3-06，技术方案 11.3）。

`urgent` 代表"要出事了"的级别，它绕过 T3-06 的 3 条上限，因此判定必须严格，否则页面
长期显示五六条紧急提示、紧急标签贬值。判定要求**同时**满足两个条件：

1. **阈值严重偏离**：排除"刚刚越过阈值"的临界情况。偏离幅度 `|value - threshold| / threshold`
   必须达到 `severe_ratio`（如 10%）。以水稻高温为例，阈值 35℃，若 35.2℃ 就报紧急，
   整个夏季首页会天天有紧急提示。
2. **当前处于敏感生育期**：同样 40℃ 在分蘖期与抽穗扬花期造成的损失量级完全不同，
   只有落在该流程的 `sensitive_stages` 名单里才算第二条命中。

两条都命中的情形才是真正需要当天紧急处置的组合。单条件只给 `notice`——**未配置即为
不紧急**（fail-safe）：宁可漏报紧急（降为 notice 仍会展示），不可误报紧急（绕过条数
上限并让标签贬值）。

判定参数（`threshold` / `severe_ratio` / `sensitive_stages`）写在流程配置里而非代码里，
因为这些数值需要农技人员按流程校准，且水稻与茶的阈值、敏感期名单各不相同。
"""

from app.broadcast.inputs.env_facts import EnvFacts
from app.process_config.models import ProcessConfig, UrgencySpec


def _deviation_ratio(facts: EnvFacts, spec: UrgencySpec) -> float:
    """指标相对阈值的偏离幅度（归一化）。

    高于型：`(value - threshold) / threshold`；低于型：`(threshold - value) / threshold`。
    指标缺失、阈值为零或方向非法时返回 0.0（视为未偏离），使调用方落到"不紧急"。
    """
    value = facts.values.get(spec.metric)
    if value is None or spec.threshold == 0:
        return 0.0
    if spec.direction == "above":
        return (value - spec.threshold) / abs(spec.threshold)
    return (spec.threshold - value) / abs(spec.threshold)


def _stage_in(stage: str | None, sensitive_stages: list[str]) -> bool:
    """当前生育期是否落在敏感名单内。阶段未知（`None`）一律为假。"""
    if stage is None:
        return False
    normalized = stage.strip()
    return normalized in sensitive_stages


def is_urgent(
    rule_id: str,
    facts: EnvFacts,
    process_config: ProcessConfig | None,
    *,
    stage: str | None = None,
) -> bool:
    """双条件判定：严重偏离 **且** 处于敏感生育期。缺一条即降为 `notice`。

    未在 `urgency_specs` 登记的 `rule_id` 永不为紧急（`returns False`）。`stage` 由调用方
    从档案/农事历取当前生育期后传入——规则层不查库，也不自行从月份反推阶段。
    """
    if process_config is None:
        return False
    spec = process_config.urgency_specs.get(rule_id)
    if spec is None:
        return False
    if _deviation_ratio(facts, spec) < spec.severe_ratio:
        return False
    return _stage_in(stage, spec.sensitive_stages)


__all__ = ["is_urgent"]
