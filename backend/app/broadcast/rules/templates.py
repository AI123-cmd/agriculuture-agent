"""事实模板：模板出事实（T3-07，技术方案 11.1 与 10.3）。

`callout / fact / impact / action` 四段的事实性内容由模板填充，模板原文即零模型模式下农户
最终看到的文案，也是模型改写的对照基线。因此**模板必须本身完整可读**——不能假设后面一定有
模型润色，零模型模式（`ZERO_MODEL_MODE=true` 或网关候选链全失败）下渲染结果就是交付物。

模板按 `rule_id` 组织，未精确命中时逐级回退：

1. 精确 `rule_id`（如 `env_risk.heat`）；
2. 去掉冒号后的任务标识（回访类 `rule_id` 形如 `{content_rule}:{task_id}`，见 T3-04）；
3. 逐级去掉末位点分段（`followup.observing.rice` → `followup.observing` → `followup`）；
4. 按 `source_kind` 取通用模板（`env_risk` / `calendar` / `followup`）。

回退到通用模板是"每条规则都有文案"的完整性保证：只要一类来源存在模板，该类下任何新
`rule_id` 都不会因为漏配而无文案。逐级回退覆盖了 T3-05 的三类产出形态（环境阈值规则的
点分标识、农事历的 `calendar:{knowledge_id}`、回访的 `{content_rule}:{task_id}`）。

**与文档示例的一处偏离**：示例里给的是 `calendar.stage` 键，但农事历条目的 `rule_id` 实际
形如 `calendar:{knowledge_id}`（见 `inputs/calendar.py`），按 `_family` 只能得到 `calendar`，
`calendar.stage` 永不会被命中——那会与"每个键都能被产出"的验收标准冲突。故此处以 `calendar`
为键，行为等价（同类来源共用一份四段模板）。

模板缺失时抛 `RuleImplementationError` 而不是回落到"直接拼四段"：回落会让模板表形同虚设，
且失去"每条规则都有对应文案"的完整性。缺配属代码缺陷，应在测试期暴露。
"""

from app.broadcast.rules.base import RuleResult
from app.broadcast.rules.errors import RuleImplementationError

# 四段式模板（含影响段）与三段式模板（回访类无影响段，见 T3-04 的 `build_followup_rule`）。
_FOUR_SECTION = "{callout}：{fact}。{impact}。{action}。"
_THREE_SECTION = "{callout}：{fact}。{action}。"

# 按 `rule_id` 与来源族组织的模板表。键的命中顺序见 `lookup_template`。
RULE_TEMPLATES: dict[str, str] = {
    # 环境阈值规则（T3-05 `catalog.rule_ids()` 的七条）。高频提示各自成句，措辞更贴场景。
    "env_risk.heat": "{callout}：{fact}。{impact}。{action}。",
    "env_risk.cold": "{callout}：{fact}。{impact}。{action}。",
    "env_risk.soil_dry": "{callout}：{fact}。{impact}。{action}。",
    "env_risk.humidity": "{callout}：{fact}。{impact}。{action}。",
    "env_risk.rain": "{callout}：{fact}。{impact}。{action}。",
    "env_risk.wind": "{callout}：{fact}。{impact}。{action}。",
    # 来源族通用模板：覆盖未单列的同类条目（如 `env_risk.pest_risk`）。
    "env_risk": _FOUR_SECTION,
    "calendar": _FOUR_SECTION,
    # 回访类：观察期问诊不回问"效果"，与回访条目共用三段式。
    "followup.observing": _THREE_SECTION,
    "followup.effect_check": _THREE_SECTION,
    "followup": _THREE_SECTION,
}

# 句末标点：渲染前去除分段自带的句末标点，避免模板再补一个造成"。。"。
_TERMINAL = "。！？；;.!?"


def template_key(item: RuleResult) -> str | None:
    """按 精确键 → 去任务标识 → 逐级去点分段 → 来源族 的顺序解析命中的模板键。

    返回的是**模板表的键**而非模板串，供交叉校验"模板表里没有永远不会被命中的死键"。
    """
    rule_id = item.rule_id
    if rule_id in RULE_TEMPLATES:
        return rule_id
    base = rule_id.split(":", 1)[0]
    if base != rule_id and base in RULE_TEMPLATES:
        return base
    parts = base.split(".")
    while len(parts) > 1:
        parts.pop()
        candidate = ".".join(parts)
        if candidate in RULE_TEMPLATES:
            return candidate
    return item.source_kind if item.source_kind in RULE_TEMPLATES else None


def lookup_template(item: RuleResult) -> str | None:
    """取模板串。命中键见 `template_key`，未命中返回 `None`。"""
    key = template_key(item)
    return RULE_TEMPLATES[key] if key is not None else None


def _clean(segment: str) -> str:
    """去掉分段首尾空白与自带句末标点，供模板统一补一个句号。"""
    return segment.strip().rstrip(_TERMINAL)


def render_template(item: RuleResult) -> str:
    """模板原文：零模型模式下的最终文案，也是模型输出的对照基线。

    模板缺失时抛 `RuleImplementationError`；四段里未被模板引用的段（如三段式模板不引用
    `impact`）不参与渲染，`str.format` 的多余关键字参数被忽略。
    """
    tpl = lookup_template(item)
    if tpl is None:
        raise RuleImplementationError(f"{item.rule_id} 缺少文案模板")
    return tpl.format(
        callout=_clean(item.callout),
        fact=_clean(item.fact),
        impact=_clean(item.impact),
        action=_clean(item.action),
    )


__all__ = [
    "RULE_TEMPLATES",
    "lookup_template",
    "render_template",
    "template_key",
]
