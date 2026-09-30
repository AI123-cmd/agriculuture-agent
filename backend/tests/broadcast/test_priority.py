"""T3-06 验收：优先级排序与条数控制。

覆盖验收标准的六个点：普通提示限 3 条且被截断项仍返回、紧急不受限、双条件 `is_urgent`
（严重偏离 且 敏感生育期）、合并去重保留最高级、排序稳定且 `urgent` 恒在最前、以及
`priority_key` 的形状与符号。全部是纯函数用例，不依赖数据库与模型。

双条件用例针对 `rice.yaml` / `tea.yaml` 里的真实 `urgency_specs`（阈值 35℃、水稻敏感期含
抽穗扬花期），而不是测试里另造一份配置——配置错配正是"紧急判定过松"最可能的来源，必须让
真实配置进入断言。
"""

from app.agent.state import ProcessId
from app.broadcast.inputs.env_facts import EnvFacts
from app.broadcast.priority import (
    NORMAL_LIMIT,
    SEVERITY_WEIGHT,
    apply_limit,
    merge_and_rank,
    priority_key,
    priority_score,
    rank_and_limit,
)
from app.broadcast.rules import RuleResult, is_urgent
from app.process_config.loader import load_process_config

RICE = load_process_config(ProcessId.RICE)
TEA = load_process_config(ProcessId.TEA)


def _item(
    rule_id: str,
    *,
    severity: str = "notice",
    timeliness: float = 0.0,
    relevance: float = 0.0,
) -> RuleResult:
    return RuleResult(
        source_kind="env_risk",
        severity=severity,
        callout="水稻",
        fact=f"{rule_id} 的事实",
        impact="影响",
        action="行动",
        rule_id=rule_id,
        timeliness_score=timeliness,
        relevance_score=relevance,
    )


# ---- 一、priority_key 的形状与符号 ----


def test_priority_key_is_three_tuple_all_non_positive() -> None:
    """返回长度为 3 的元组且三项均 ≤ 0（取负是为了降序排列）。"""
    key = priority_key(_item("a", severity="notice", timeliness=0.5, relevance=0.7))
    assert isinstance(key, tuple)
    assert len(key) == 3
    assert all(component <= 0 for component in key)


def test_priority_key_orders_severity_before_timeliness() -> None:
    """紧急程度权重高于时效性：低时效的 urgent 仍排在高效时的 notice 之前。"""
    urgent = _item("urgent", severity="urgent", timeliness=0.0)
    notice = _item("notice", severity="notice", timeliness=1.0)
    assert priority_key(urgent) < priority_key(notice)


def test_priority_key_orders_timeliness_before_relevance() -> None:
    """时效性权重高于相关度。"""
    timely = _item("t", severity="notice", timeliness=1.0, relevance=0.0)
    relevant = _item("r", severity="notice", timeliness=0.0, relevance=1.0)
    assert priority_key(timely) < priority_key(relevant)


def test_priority_score_monotonic_with_key() -> None:
    """合成分数与排序键单调一致：键越小，分数越大。"""
    urgent = _item("u", severity="urgent", timeliness=1.0, relevance=1.0)
    notice = _item("n", severity="notice", timeliness=0.0, relevance=0.0)
    assert priority_key(urgent) < priority_key(notice)
    assert priority_score(urgent) > priority_score(notice)


def test_priority_score_is_rounded_to_three_places() -> None:
    """落库列是 Numeric(8, 3)，合成分数须保留三位小数。"""
    score = priority_score(_item("a", severity="notice", timeliness=0.3333, relevance=0.6666))
    assert score == round(score, 3)


# ---- 二、合并去重 ----


def test_merge_and_rank_dedups_by_rule_id_keeping_highest() -> None:
    """同一 rule_id 的两条保留优先级最高的（urgent），而不是先出现的那条。"""
    notice = _item("dup", severity="notice")
    urgent = _item("dup", severity="urgent")
    merged = merge_and_rank([notice, urgent])
    assert len(merged) == 1
    assert merged[0].severity == "urgent"


def test_merge_and_rank_dedup_independent_of_input_order() -> None:
    """先 sorted 再 setdefault：去重结果不随输入采集顺序变化。"""
    notice = _item("dup", severity="notice")
    urgent = _item("dup", severity="urgent")
    assert merge_and_rank([notice, urgent]) == merge_and_rank([urgent, notice])


def test_merge_and_rank_is_stable_across_runs() -> None:
    """同一批条目连跑两次，返回顺序完全一致。"""
    items = [
        _item("a", severity="notice", timeliness=0.5),
        _item("b", severity="urgent"),
        _item("c", severity="reminder"),
        _item("d", severity="notice", timeliness=0.9),
    ]
    assert merge_and_rank(items) == merge_and_rank(items)


def test_merge_and_rank_puts_urgent_first() -> None:
    """排序结果中 urgent 一定排在最前。"""
    items = [
        _item("n1", severity="notice", timeliness=0.9),
        _item("u1", severity="urgent"),
        _item("r1", severity="reminder"),
    ]
    ordered = merge_and_rank(items)
    assert ordered[0].severity == "urgent"


# ---- 三、条数控制 ----


def test_six_notice_keeps_three_and_suppresses_three() -> None:
    """6 条 notice → 保留 3 条展示、3 条被抑制，且被抑制项仍在第二个列表中返回。"""
    items = [_item(f"n{i}", severity="notice", timeliness=i / 10) for i in range(6)]
    kept, dropped = apply_limit(items)
    assert len(kept) == NORMAL_LIMIT == 3
    assert len(dropped) == 3
    assert all(flag is True for _, flag in dropped)
    assert {item.rule_id for item, _ in dropped}.isdisjoint({i.rule_id for i in kept})


def test_five_notice_keeps_three() -> None:
    """5 条 notice 无 urgent → 展示条目数为 3。"""
    items = [_item(f"n{i}") for i in range(5)]
    kept, dropped = apply_limit(items)
    assert len(kept) == 3
    assert len(dropped) == 2


def test_urgent_not_limited() -> None:
    """3 条 notice 加 2 条 urgent → 展示条目数为 5（urgent 不参与截断）。"""
    items = [_item(f"n{i}") for i in range(3)] + [_item(f"u{i}", severity="urgent") for i in range(2)]
    kept, dropped = apply_limit(items)
    assert len(kept) == 5
    assert dropped == []


def test_urgent_alone_is_never_truncated() -> None:
    """全是 urgent 时不截断、不抑制。"""
    items = [_item(f"u{i}", severity="urgent") for i in range(7)]
    kept, dropped = apply_limit(items)
    assert len(kept) == 7
    assert dropped == []


def test_rank_and_limit_composes_dedup_and_limit() -> None:
    """组合入口：先去重再截断，重复的 rule_id 不占两个名额。"""
    items = [_item("dup", severity="notice")] * 2 + [_item(f"n{i}") for i in range(4)]
    kept, dropped = rank_and_limit(items)
    assert len(kept) == 3
    assert len(dropped) == 2


# ---- 四、双条件 is_urgent（用真实流程配置） ----


def test_severity_weight_covers_three_levels() -> None:
    assert SEVERITY_WEIGHT == {"urgent": 3, "notice": 2, "reminder": 1}


def test_heat_urgent_when_severe_and_sensitive_stage() -> None:
    """水稻 40℃（偏离 35℃ 超 10%）且处于抽穗扬花期 → urgent。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 40.0})
    assert is_urgent("env_risk.heat", facts, RICE, stage="抽穗扬花期") is True


def test_heat_not_urgent_in_insensitive_stage() -> None:
    """同样 40℃ 但处于分蘖期（不在敏感名单）→ 单条件不成立，非 urgent。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 40.0})
    assert is_urgent("env_risk.heat", facts, RICE, stage="分蘖期") is False


def test_heat_not_urgent_at_critical_value() -> None:
    """35.2℃（仅轻微越过 35℃）即使处于抽穗扬花期 → 偏离不足，非 urgent。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 35.2})
    assert is_urgent("env_risk.heat", facts, RICE, stage="抽穗扬花期") is False


def test_unregistered_rule_is_never_urgent() -> None:
    """未在 urgency_specs 登记的 rule_id 永不为 urgent（fail-safe）。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 40.0})
    assert is_urgent("env_risk.wind", facts, RICE, stage="抽穗扬花期") is False


def test_no_config_is_never_urgent() -> None:
    """无流程配置（配置不可用）时不判紧急。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 40.0})
    assert is_urgent("env_risk.heat", facts, None, stage="抽穗扬花期") is False


def test_missing_stage_is_never_urgent() -> None:
    """生育期未知时不判紧急：无法证明"当前处于敏感期"。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 40.0})
    assert is_urgent("env_risk.heat", facts, RICE, stage=None) is False


def test_tea_sensitive_stage_differs_from_rice() -> None:
    """茶树与水稻的敏感期名单不同：同一 40℃ 在水稻抽穗扬花期紧急、在茶树该期不紧急。"""
    facts = EnvFacts(source="sensor", values={"air_temp": 40.0})
    assert is_urgent("env_risk.heat", facts, RICE, stage="抽穗扬花期") is True
    assert is_urgent("env_risk.heat", facts, TEA, stage="抽穗扬花期") is False
    assert is_urgent("env_risk.heat", facts, TEA, stage="新梢生长期") is True
