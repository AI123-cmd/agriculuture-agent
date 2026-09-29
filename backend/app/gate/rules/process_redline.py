"""R-REDLINE 流程专属红线（T2-42）。

命中当前流程 `redlines` 配置中任一禁用项即拦截。它是"红线由配置承载而非代码分支"这条设计
（技术方案 8.4）在运行时的执行者——禁令措辞随流程与季节变化，写进 YAML 才能改配置即生效。

三个扫描位置是必要的：展示文本是模型改写后的成品，动作文本是结构化的原文，`detail_terms`
是术语到口语化的改写映射。只扫展示文本会漏掉后两类。
"""

from typing import ClassVar

from app.gate.contracts import GateContext, GateViolation
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.matching import normalize_text
from app.gate.rules.base import Rule, register


@register
class ProcessRedlineRule(Rule):
    """R-REDLINE：命中当前流程红线配置的任一禁用项即拦截。"""

    rule_id = GateRuleId.REDLINE.value
    severity: ClassVar[GateSeverity] = "block"
    description = "不得触碰本流程的禁用输出清单"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        labels = ctx.redlines.blocking_labels()
        if not labels:
            return violations
        text = ctx.payload.rendered_text
        for label in labels:
            if not label:
                continue  # 空标签会让 `label in text` 恒真，造成全量拦截
            hit = self._locate(text, label, ctx)
            if hit is None:
                continue
            field, snippet = hit
            rule_ref = self._rule_ref(ctx, label)
            violations.append(
                self.violation(
                    field=field,
                    message=f"命中本流程禁用项「{label}」（{rule_ref}）：{snippet}",
                )
            )
        return violations

    @staticmethod
    def _locate(text: str, label: str, ctx: GateContext) -> tuple[str, str] | None:
        """在展示文本、动作文本与术语改写结果中定位禁用项。返回 (字段, 片段)。"""
        if _find(text, label):
            return "gate_payload.rendered_text", _snippet(text, label)
        for i, action in enumerate(ctx.prescription.actions):
            joined = " ".join(
                [action.action, action.timing, action.method, *action.cautions, *action.stop_conditions]
            )
            if _find(joined, label):
                return f"actions[{i}]", _snippet(joined, label)
        for term, rewritten in ctx.prescription.detail_terms.items():
            if _find(rewritten, label):
                return f"detail_terms[{term}]", _snippet(rewritten, label)
        return None

    @staticmethod
    def _rule_ref(ctx: GateContext, label: str) -> str:
        """反查该禁用项所属的红线 id，便于审计追溯到具体配置条目。"""
        for redline in ctx.redlines.redlines:
            if label in redline.labels():
                return f"{redline.id} / {redline.desc}"
        return "未归属到具体红线"


def _find(text: str, label: str) -> bool:
    """归一化子串匹配。`label in text` 会漏掉全半角差异与空格插入。"""
    return normalize_text(label) in normalize_text(text)


def _snippet(text: str, label: str, radius: int = 12) -> str:
    """截取命中位置的上下文，供审计表记录（不含会话原文，仅展示文本）。"""
    pos = text.find(label)
    if pos < 0:
        pos = normalize_text(text).find(normalize_text(label))
        if pos < 0:
            return label
    start = max(0, pos - radius)
    end = min(len(text), pos + len(label) + radius)
    return f"...{text[start:end]}..."


__all__ = ["ProcessRedlineRule"]
