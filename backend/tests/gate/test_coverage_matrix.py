"""T6-03：闸门十二类规则的覆盖矩阵（每条至少 1 正例 1 反例）。

各规则的行为细节由 `rules/test_*.py` 覆盖，本文件只回答一个聚合问题：**十二条规则里，
有哪一条缺了正例或缺了反例**。因此这里不重复 `check` 内部逻辑的断言，只用"该拦的拦住、
不该拦的放行"这一个判据把所有规则摆到同一张表里，缺一条就在参数化列表里显形。

正例（`_BLOCKS`）：构造明确违规的 `GateContext`，断言命中该规则且等级与 `Rule.severity`
一致。等级写错会让引擎的裁定聚合失效（block 被当成 warn 就放行了），所以这里连等级一起断言。

反例（`_PASSES`）：构造**擦边合规**的输入——阈值恰好落在边界上，而不是"明显没事"。
例如 `R-INTERVAL` 的反例是"间隔期恰等于下限"、`R-DOSAGE` 是"剂量恰等于上限"。若把边界
写成"远低于/远高于"，用例就无法发现"阈值比较方向写反"这类缺陷（`R-INTERVAL` 的验收标准
明确要求做这个替换验证）。

**三轨验证（技术方案 14.2）**——本项只是第一轨，三轨是递进而非并列：

```python
TRACKS = {
    "rule_unit": "T6-03 本项：CI 期。每条规则正例/反例，验证规则本身写对了",
    "shadow":    "T2-46 影子模式：灰度期。GATE_MODE=shadow，只记不拦，人工抽查误杀率",
    "enforce":   "全量期。误杀率达标后切 GATE_MODE=enforce，生产启动检查拒绝 shadow",
}
```

单测全绿**不构成**开启强制拦截的依据：它只证明"规则在构造输入上正确"，证明不了"真实流量
上不会大面积误杀"（那是影子模式的职责）。故本文件不校验 `GATE_MODE`，只校验覆盖度。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.gate.contracts import GateContext, GatePayload
from app.gate.enums import GateRuleId, ProcessId
from app.gate.rules.base import build_rules
from tests.gate.fixtures.factories import (
    gate_context,
    medication_record,
    pesticide_item,
    prescription,
)
from tests.gate.fixtures.fake_knowledge import (
    FakeEntry,
    FakeKnowledge,
    loaded_redlines,
    unavailable_redlines,
)

TRACKS: dict[str, str] = {
    "rule_unit": "T6-03 本项：CI 期。每条规则正例/反例，验证规则本身写对了",
    "shadow": "T2-46 影子模式：灰度期。GATE_MODE=shadow，只记不拦，人工抽查误杀率",
    "enforce": "全量期。误杀率达标后切 GATE_MODE=enforce，生产启动检查拒绝 shadow",
}

TODAY = date(2025, 6, 1)
SEASON_START = date(2025, 5, 1)


# ---- 正例：该拦的拦住。每个构造函数返回一个"确定违规"的 GateContext ----


def _whitelist_violating() -> GateContext:
    """`knowledge_id` 不在已核准库中（模型越界）。"""
    return gate_context(
        knowledge=FakeKnowledge({}),
        prescription=prescription(items=[pesticide_item(knowledge_id="K-9999", name="某除草剂")]),
    )


def _registered_crop_violating() -> GateContext:
    """茶流程用只登记在水稻上的药剂。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", registered_crops=("水稻", "小麦"))}
    )
    return gate_context(
        process_id=ProcessId.TEA,
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )


def _dosage_violating() -> GateContext:
    """剂量上限 45 高于登记上限 30。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", dosage_min=20.0, dosage_max=30.0)}
    )
    return gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(dosage_min=20.0, dosage_max=45.0)]),
    )


def _max_use_violating() -> GateContext:
    """登记上限 3 次，处方声明 4 次。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", max_uses_per_season=3)})
    return gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(max_uses_per_season=4)]),
        rule_configs={GateRuleId.MAX_USE.value: {"limit": 3}},
    )


def _interval_violating() -> GateContext:
    """涉及用药但顶层未给出安全间隔期。"""
    return gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=None),
        today=TODAY,
        slots={"next_pick_date": TODAY + timedelta(days=10)},
    )


def _conflict_violating() -> GateContext:
    """本季内用过同名药剂。"""
    return gate_context(
        medication_history=[medication_record(pesticide_name="吡虫啉", used_on=date(2025, 5, 20))],
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(name="吡虫啉")]),
    )


def _region_violating() -> GateContext:
    """条目适用范围 320000 不覆盖农户 330100。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", region_scope="320000")})
    return gate_context(
        region_code="330100",
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001")]),
    )


def _provenance_violating() -> GateContext:
    """展示文本含 50克 而无任何溯源登记。"""
    return gate_context(payload=GatePayload(rendered_text="每亩用50克喷雾"), numeric_tokens={})


def _no_data_violating() -> GateContext:
    """`safety_interval_days=7` 但溯源表为空。"""
    return gate_context(
        prescription=prescription(
            items=[
                pesticide_item(dosage_min=None, dosage_max=None, safety_interval_days=None, max_uses_per_season=None)
            ],
            safety_interval_days=7,
            facts_provenance={},
        ),
    )


def _process_redline_violating() -> GateContext:
    """展示文本含流程禁用品种"乐果"。"""
    return gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"], redline_id="TEA-02"),
        payload=GatePayload(rendered_text="可选用乐果防治茶树小绿叶蝉。"),
    )


def _redline_load_violating() -> GateContext:
    """红线配置加载失败。"""
    return gate_context(redlines=unavailable_redlines(ProcessId.TEA, reason="redlines 为空"))


def _caution_violating() -> GateContext:
    """强制命令语气。"""
    return gate_context(payload=GatePayload(rendered_text="发现虫害建议立即大量喷洒药剂。"))


# ---- 反例：不该拦的放行。构造必须是"擦边"而非"明显合规" ----


def _whitelist_passing() -> GateContext:
    """反例：条目在库且已核准。"""
    return gate_context(prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]))


def _registered_crop_passing() -> GateContext:
    """擦边：茶流程用登记作物含"茶"的药剂。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", registered_crops=("茶",))})
    return gate_context(
        process_id=ProcessId.TEA,
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )


def _dosage_passing() -> GateContext:
    """擦边：剂量上限恰等于登记上限 30。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", dosage_min=20.0, dosage_max=30.0)}
    )
    return gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(dosage_min=20.0, dosage_max=30.0)]),
    )


def _max_use_passing() -> GateContext:
    """擦边：处方声明次数恰等于登记上限 3。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", max_uses_per_season=3)})
    return gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(max_uses_per_season=3)]),
        medication_history=[],
    )


def _interval_passing() -> GateContext:
    """擦边：茶流程间隔期恰等于到采摘的天数（7 天），不冲突。"""
    return gate_context(
        process_id=ProcessId.TEA,
        prescription=prescription(items=[pesticide_item()], safety_interval_days=7),
        today=TODAY,
        slots={"next_pick_date": TODAY + timedelta(days=7)},
    )


def _conflict_passing() -> GateContext:
    """擦边：本季用过同类但非重复、非禁忌的另一种药剂。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", forbidden_combinations=("代森锰锌",))}
    )
    return gate_context(
        knowledge=knowledge,
        medication_history=[medication_record(pesticide_name="阿维菌素", used_on=date(2025, 5, 20))],
        season_start=SEASON_START,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )


def _region_passing() -> GateContext:
    """擦边：条目范围 330000（省）覆盖农户 330100（市）。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", region_scope="330000")})
    return gate_context(
        region_code="330100",
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001")]),
    )


def _provenance_passing() -> GateContext:
    """擦边：文本里的 30克 已在 numeric_tokens 中登记。"""
    return gate_context(numeric_tokens={"30克": "pesticide_items[0].dosage_max"})


def _no_data_passing() -> GateContext:
    """擦边：`safety_interval_days=7` 且已在 facts_provenance 登记。"""
    return gate_context(
        prescription=prescription(
            items=[
                pesticide_item(dosage_min=None, dosage_max=None, safety_interval_days=None, max_uses_per_season=None)
            ],
            safety_interval_days=7,
            facts_provenance={"safety_interval_days": "knowledge:K-1001"},
        ),
    )


def _process_redline_passing() -> GateContext:
    """反例（已知边界）：文本不含禁用清单中的任何措辞即放行。

    这里刻意只测"不在清单内即放行"，**不测**"同义改写能否拦"。规则是关键词/清单纯函数，
    同义改写不会被它拦住，这是设计事实而非缺陷；若在此假定能拦，执行者会误以为需要放宽
    匹配，进而引入误杀（T6-03 实现要点三对此有明确说明）。
    """
    return gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"]),
        payload=GatePayload(rendered_text="建议在晴天傍晚喷施低毒杀虫剂。"),
    )


def _redline_load_passing() -> GateContext:
    """反例：红线配置正常加载。"""
    return gate_context(redlines=loaded_redlines(ProcessId.RICE, forbidden=["乐果"]))


def _caution_passing() -> GateContext:
    """反例：建议性措辞。"""
    return gate_context(payload=GatePayload(rendered_text="建议在晴天下午喷施。"))


def _case(rule_id: str, ctx_factory, severity: str) -> pytest.ParameterSet:
    return pytest.param(rule_id, ctx_factory, severity, id=rule_id)


# 十二条规则各一条正例。等级必须与规则自身声明一致（写错会让聚合失效）。
_BLOCKS = [
    _case(GateRuleId.WHITELIST.value, _whitelist_violating, "block"),
    _case(GateRuleId.REGISTERED_CROP.value, _registered_crop_violating, "block"),
    _case(GateRuleId.DOSAGE.value, _dosage_violating, "block"),
    _case(GateRuleId.MAX_USE.value, _max_use_violating, "block"),
    _case(GateRuleId.INTERVAL.value, _interval_violating, "block"),
    _case(GateRuleId.CONFLICT.value, _conflict_violating, "block"),
    _case(GateRuleId.REGION.value, _region_violating, "degrade"),
    _case(GateRuleId.PROVENANCE.value, _provenance_violating, "block"),
    _case(GateRuleId.NO_DATA.value, _no_data_violating, "block"),
    _case(GateRuleId.REDLINE.value, _process_redline_violating, "block"),
    _case(GateRuleId.REDLINE_LOAD.value, _redline_load_violating, "block"),
    _case(GateRuleId.CAUTION.value, _caution_violating, "warn"),
]

# 十二条规则各一条反例。
_PASSES = [
    _case(GateRuleId.WHITELIST.value, _whitelist_passing, "block"),
    _case(GateRuleId.REGISTERED_CROP.value, _registered_crop_passing, "block"),
    _case(GateRuleId.DOSAGE.value, _dosage_passing, "block"),
    _case(GateRuleId.MAX_USE.value, _max_use_passing, "block"),
    _case(GateRuleId.INTERVAL.value, _interval_passing, "block"),
    _case(GateRuleId.CONFLICT.value, _conflict_passing, "block"),
    _case(GateRuleId.REGION.value, _region_passing, "degrade"),
    _case(GateRuleId.PROVENANCE.value, _provenance_passing, "block"),
    _case(GateRuleId.NO_DATA.value, _no_data_passing, "block"),
    _case(GateRuleId.REDLINE.value, _process_redline_passing, "block"),
    _case(GateRuleId.REDLINE_LOAD.value, _redline_load_passing, "block"),
    _case(GateRuleId.CAUTION.value, _caution_passing, "warn"),
]


def _hits(rule_id: str, ctx: GateContext) -> list:
    """只跑这一条规则，返回它的违规列表。规则是纯函数，无需引擎、无库无网。"""
    (rule,) = build_rules([rule_id])
    return rule.check(ctx)


@pytest.mark.parametrize(("rule_id", "ctx_factory", "severity"), _BLOCKS)
def test_rule_blocks_when_should(rule_id: str, ctx_factory, severity: str) -> None:
    """正例：明确违规时命中该规则，且命中项的等级与规则声明的等级一致。"""
    violations = _hits(rule_id, ctx_factory())

    assert violations, f"{rule_id} 未命中，期望按 {severity} 拦截"
    assert {v.rule_id for v in violations} == {rule_id}
    assert all(v.severity == severity for v in violations), f"{rule_id} 等级应为 {severity}"


@pytest.mark.parametrize(("rule_id", "ctx_factory", "severity"), _PASSES)
def test_rule_passes_when_compliant(rule_id: str, ctx_factory, severity: str) -> None:
    """反例（擦边）：合规输入下该规则不命中，证明它不误拦。"""
    violations = _hits(rule_id, ctx_factory())

    assert not any(v.rule_id == rule_id for v in violations), (
        f"{rule_id} 误拦合规输入：{[v.message for v in violations]}"
    )


def test_matrix_covers_every_rule_id() -> None:
    """矩阵必须恰好覆盖十二条规则，正反例成对出现（缺一条即失败）。"""
    declared = {r.value for r in GateRuleId}
    positives = {case.values[0] for case in _BLOCKS}
    negatives = {case.values[0] for case in _PASSES}

    assert positives == declared, f"缺正例的规则：{declared - positives}"
    assert negatives == declared, f"缺反例的规则：{declared - negatives}"


def test_three_tracks_are_documented() -> None:
    """三轨判据齐全：单测 / 影子 / 强制执行各有说明，缺一即说明文档漂移。"""
    assert set(TRACKS) == {"rule_unit", "shadow", "enforce"}
    assert all(TRACKS.values())
