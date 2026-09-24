"""养猪流程的识别信号表与匹配逻辑（T2-22）。

报告守卫的判定全部落在这里的三个纯函数上：`match_signals`（命中哪些信号）、
`resolve_trigger_source`（最严重的那一条是什么等级）与两个字符串辅助函数。它们不调模型、
不读数据库、不依赖上下文，因此守卫可以被完全单测，也可以在检查点恢复后确定性重放。

**为什么信号既查槽位也查原文**：技术方案 7.4 要求养猪流程的报告守卫插在 `observe`
**之前**（T2-28 的入口守卫）。那一刻问诊尚未开始，`slots` 是空的，只查槽位的信号表在
最需要触发的场景下全部哑火。因此同一信号配两条规则：`source="slot"` 读问诊后的结构化
答案，`source="text"` 读农户第一句描述。两者命中同一 `signal_id`，去重后只报一次。

**为什么槽位 key 必须逐字对齐 `pig.yaml`**：`match_signals` 对未填写或缺失的槽位直接跳过，
槽位 key 写错不会报错，只会让这条信号永远不触发——这正是守卫最危险的失效形态（漏报无声）。
`pig.yaml` 现有的信号槽位是 `pig_symptom_pattern`、`pig_death_count`、`pig_sudden_death`、
`pig_multi_pen`；其余信号（流产）没有对应槽位，只走文本规则。

关键词表属农技口径，需人工确认：本模块按清单录入，不自行扩展语义。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.agent.contracts import ObservationResult
from app.agent.state import ConsultationState

Severity = Literal["signal", "group", "mass_death"]
SignalSource = Literal["slot", "text", "observation"]
TriggerSourceValue = Literal["signal", "group", "mass_death", "none"]


class SignalRule(BaseModel):
    """一条识别信号规则。`keywords` 命中任一即触发，不做数量门槛。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str
    description: str
    keywords: tuple[str, ...]
    slot_key: str | None = None
    severity: Severity = "signal"
    source: SignalSource = "slot"


# (signal_id, 描述, 关键词, 槽位 key, 等级)。槽位 key 与 `pig.yaml` 的 `required_slots` 对应。
_SLOT_SIGNAL_TABLE: tuple[tuple[str, str, tuple[str, ...], str, Severity], ...] = (
    (
        "sudden_death",
        "无明显诱因突然死亡",
        ("突然死", "猝死", "无先兆"),
        "pig_sudden_death",
        "signal",
    ),
    (
        "skin_hemorrhage",
        "皮肤出现出血点或紫斑",
        ("出血点", "紫斑", "发紫", "皮肤发红发紫"),
        "pig_symptom_pattern",
        "signal",
    ),
    (
        "high_fever",
        "持续高热",
        ("发烧", "高烧", "体温高"),
        "pig_symptom_pattern",
        "signal",
    ),
    (
        "multi_pen",
        "多个栏舍同时出现相似症状",
        ("多个栏舍同时出现", "两三个栏", "好几个栏"),
        "pig_multi_pen",
        "group",
    ),
    (
        "mass_death",
        "短期内群体性死亡",
        ("5头以上", "五头以上", "大量死亡"),
        "pig_death_count",
        "mass_death",
    ),
)

# 文本规则：读农户的第一句描述。与槽位规则是**并集**关系，同一 signal_id 去重后只报一次。
# `abortion` 没有对应槽位（`pig.yaml` 未配该项），因此只在文本规则里存在。
_TEXT_SIGNAL_TABLE: tuple[tuple[str, str, tuple[str, ...], str, Severity], ...] = (
    (
        "sudden_death",
        "无明显诱因突然死亡",
        ("突然死", "猝死", "一夜之间", "早上还好好的", "昨天还好好的", "昨天死了", "死了一头", "无先兆"),
        "",
        "signal",
    ),
    (
        "skin_hemorrhage",
        "皮肤出现出血点或紫斑",
        ("出血点", "紫斑", "发紫", "身上有红点", "皮肤发红"),
        "",
        "signal",
    ),
    (
        "high_fever",
        "持续高热",
        ("高烧", "发烧不退", "体温高", "一直发热"),
        "",
        "signal",
    ),
    (
        "abortion",
        "母猪流产或死胎",
        ("流产", "死胎", "早产"),
        "",
        "signal",
    ),
    (
        "multi_pen",
        "多个栏舍同时出现相似症状",
        ("好几个栏", "几栋都有", "相邻栏", "好几个圈"),
        "",
        "group",
    ),
    (
        "mass_death",
        "短期内群体性死亡",
        ("死了好多", "成片死", "大量死亡", "全死了", "死了一大片"),
        "",
        "mass_death",
    ),
)


def _slot_rules() -> tuple[SignalRule, ...]:
    return tuple(
        SignalRule(signal_id=s, description=d, keywords=k, slot_key=sk, severity=lv, source="slot")
        for s, d, k, sk, lv in _SLOT_SIGNAL_TABLE
    )


def _text_rules() -> tuple[SignalRule, ...]:
    return tuple(
        SignalRule(signal_id=s, description=d, keywords=k, slot_key=None, severity=lv, source="text")
        for s, d, k, _sk, lv in _TEXT_SIGNAL_TABLE
    )


PIG_SIGNALS: tuple[SignalRule, ...] = _slot_rules()
PIG_TEXT_SIGNALS: tuple[SignalRule, ...] = _text_rules()
# 默认规则集是两者的并集：入口守卫（观察之前）只能靠文本规则，兜底守卫（问诊之后）主要靠槽位
# 规则，两者都要在同一个判据里生效。
SIGNAL_RULES: tuple[SignalRule, ...] = PIG_SIGNALS + PIG_TEXT_SIGNALS

# 肯定回答的字面值。槽位提问允许"是/否"式作答，此时值本身不含任何症状关键词，但仍构成命中。
_AFFIRMATIVE = frozenset({"是", "有", "出现", "已出现", "是的"})


def match_signals(
    state: ConsultationState,
    observations: ObservationResult | None,
    rules: tuple[SignalRule, ...] = SIGNAL_RULES,
) -> list[str]:
    """返回命中的信号 id 列表。任一命中即触发，不做数量门槛。

    纯字符串包含，不调用模型：技术方案 5.1 把报告守卫的自治程度标为"极低（规则优先）"，
    6.5 的独立性表明确要求不调用模型。即使模型认为"这一例不严重"，也不能阻止信号命中后的触发。
    `_contains_any` 与 `_is_affirmative` 是并集关系，同时支持选项式提问与自由描述。
    """
    slots = state.get("slots") or {}
    text = state.get("user_message") or ""
    facts = _observation_text(observations)
    hits: list[str] = []
    for rule in rules:
        if rule.source == "slot" and rule.slot_key:
            slot = slots.get(rule.slot_key)
            if slot is None or slot.status != "filled" or not slot.value:
                continue
            if _contains_any(slot.value, rule.keywords) or _is_affirmative(slot.value):
                _append(hits, rule.signal_id)
        elif rule.source == "text" and _contains_any(text, rule.keywords):
            _append(hits, rule.signal_id)
        elif rule.source == "observation" and _contains_any(facts, rule.keywords):
            _append(hits, rule.signal_id)
    return hits


def resolve_trigger_source(hits: list[str], rules: tuple[SignalRule, ...] = SIGNAL_RULES) -> TriggerSourceValue:
    """按最严重的一条信号决定 `trigger_source`。未命中返回 `none`。

    `trigger_source` 记录的是**最严重的命中等级**，而不是"命中了哪一级才算触发"。数量或
    范围信息（group、mass_death）用于让输出文案更贴合实际情况，不用于抬升触发门槛。
    """
    if not hits:
        return "none"
    severity_of = {r.signal_id: r.severity for r in rules}
    severities = [severity_of.get(h, "signal") for h in hits]
    if "mass_death" in severities:
        return "mass_death"
    if "group" in severities:
        return "group"
    return "signal"


def _append(hits: list[str], signal_id: str) -> None:
    """保序去重：同一信号由槽位规则与文本规则各命中一次时只报一次，顺序由规则表决定。"""
    if signal_id not in hits:
        hits.append(signal_id)


def _observation_text(observations: ObservationResult | None) -> str:
    """现场事实合成一段可匹配文本。无现场数据时返回空串，匹配自然不命中。"""
    if observations is None:
        return ""
    return "；".join(value for value in observations.observed_facts.values() if value)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _is_affirmative(value: str) -> bool:
    """槽位值本身即为肯定回答时命中（如 options 为是否式提问时值为"是"）。"""
    return value.strip() in _AFFIRMATIVE


__all__ = [
    "PIG_SIGNALS",
    "PIG_TEXT_SIGNALS",
    "SIGNAL_RULES",
    "Severity",
    "SignalRule",
    "SignalSource",
    "match_signals",
    "resolve_trigger_source",
]
