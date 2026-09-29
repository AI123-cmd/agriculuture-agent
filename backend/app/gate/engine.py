"""闸门规则引擎（T2-34，技术方案 8.2/8.4/8.5）。

规则执行与结果聚合。无状态，可跨请求复用。

`_run` 的异常兜底是 fail-closed 在引擎层的执行点。生成链路遇异常可降级但仍服务（全局约定
第六节），闸门不可以：规则抛异常意味着"没能完成校验"，唯一安全结论是不放行；用 `Exception`
粒度而非窄类型捕获，是因为规则里任何一处 `KeyError`、`TypeError` 都会让校验半途而废，而
半途而废的校验看起来像通过。

`_gate_open` 短路是"红线缺失即不可用"的工程落地：`R-REDLINE-LOAD` 排序第一，一旦 block
后续规则整体跳过，代价是 `checked_rules` 变短，正好被 8.5 第三条复盘用途当作信号。
"""

from typing import Literal

from app.gate.constants import DEGRADE_OUTPUT_TEXT
from app.gate.contracts import GateContext, GateResult, GateViolation
from app.gate.enums import GateDecision, GateRuleId
from app.gate.rules.base import Rule, build_rules

# 裁定档位的字面量类型，与 `GateResult.decision` 的声明一致。
Decision = Literal["allow", "degrade", "block"]

# 前置规则：必须最先执行，其结论决定其余规则是否可信。红线未加载时它是唯一的执行者。
_PRECHECK_RULE_IDS = (GateRuleId.REDLINE_LOAD.value,)


class GateEngine:
    """规则执行与结果聚合。无状态，可跨请求复用。"""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        # 触发规则包导入，确保注册表已填充十二条规则（`app.gate.rules` 的导入副作用）。
        import app.gate.rules  # noqa: F401

        self._rules = build_rules() if rules is None else rules
        order = {rule_id: i for i, rule_id in enumerate(_PRECHECK_RULE_IDS)}
        self._rules.sort(key=lambda r: (order.get(r.rule_id, len(order)), r.rule_id))

    def evaluate(self, ctx: GateContext) -> GateResult:
        """执行全部规则并裁定。任何异常一律 fail-closed。"""
        checked: list[str] = []
        violations: list[GateViolation] = []
        for rule in self._rules:
            if rule.rule_id in _PRECHECK_RULE_IDS or self._gate_open(ctx, violations):
                checked.append(rule.rule_id)
                violations.extend(self._run(rule, ctx))
            if not ctx.redlines.loaded:
                break  # 红线未加载：前置规则跑完即停，不再执行其余规则
        decision = self._decide(violations)
        return GateResult(
            passed=decision == GateDecision.ALLOW.value,
            violations=violations,
            checked_rules=checked,
            process_redlines_loaded=ctx.redlines.loaded,
            decision=decision,
            degrade_output=DEGRADE_OUTPUT_TEXT if decision == GateDecision.DEGRADE.value else None,
        )

    def uncovered_rule_ids(self, ctx: GateContext, result: GateResult) -> list[str]:
        """返回声明了但未执行的规则 id。红线未加载时短路导致的缺口不算缺口。

        8.5 第三条复盘用途：定期核对 `checked_rules` 是否覆盖声明的全部规则 id，
        发现"声明了但没执行"是配置错误。见 T2-43 对两种"未覆盖"的区分。
        """
        if not result.process_redlines_loaded:
            return []
        declared = set(ctx.redlines.declared_rule_ids())
        return sorted(declared - set(result.checked_rules))

    def _run(self, rule: Rule, ctx: GateContext) -> list[GateViolation]:
        """单条规则执行。抛异常即视为判定失败，按最保守方向记一条 block。"""
        try:
            return rule.check(ctx)
        except Exception as exc:  # noqa: BLE001 —— 闸门必须吞掉一切异常
            return [
                GateViolation(
                    rule_id=rule.rule_id,
                    severity="block",
                    field="-",
                    message=f"规则执行异常，按最保守判定拦截：{exc}",
                )
            ]

    @staticmethod
    def _gate_open(ctx: GateContext, violations: list[GateViolation]) -> bool:
        """红线未加载或已出现 block 级违规时，后续规则不再执行。"""
        return ctx.redlines.loaded and not any(v.severity == "block" for v in violations)

    @staticmethod
    def _decide(violations: list[GateViolation]) -> Decision:
        """聚合裁定：block 优先于 degrade，degrade 优先于 warn，其余放行。"""
        if any(v.severity == "block" for v in violations):
            return "block"
        if any(v.severity == "degrade" for v in violations):
            return "degrade"
        return "allow"


__all__ = ["GateEngine"]
