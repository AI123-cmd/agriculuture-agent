"""T2-35：`R-WHITELIST` 药剂白名单。

这条规则回答"处方里写的药剂，知识库里到底有没有、能不能用、名字对不对"。它与
`R-REGISTERED-CROP`（登记作物）、`R-REDLINE`（禁用品种）都读同一份 `pesticide_items`，
但各自只回答一个问题：单条规则失败时提示语能直接指向一个具体原因。因此本文件只覆盖
"条目存在性 / 状态 / 名一致性"三件事，不重复覆盖剂量、登记作物、禁用文本。
"""

from pathlib import Path

import pytest

from app.gate.enums import GateRuleId
from app.gate.rules.base import build_rules
from app.gate.rules.pesticide_whitelist import PesticideWhitelistRule
from tests.gate.fixtures.factories import gate_context, pesticide_item, prescription
from tests.gate.fixtures.fake_knowledge import FakeEntry, FakeKnowledge

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "pesticide_whitelist.py"


def _run(ctx):
    return PesticideWhitelistRule().check(ctx)


# ---- 正例 ----


def test_two_approved_entries_pass() -> None:
    """正例 1：两个药剂条目均在库中且均为 approved，返回空列表。"""
    knowledge = FakeKnowledge(
        {
            "K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉"),
            "K-2002": FakeEntry(knowledge_id="K-2002", name="代森锰锌", status="approved"),
        }
    )
    ctx = gate_context(
        knowledge=knowledge,
        prescription=prescription(
            items=[
                pesticide_item(knowledge_id="K-1001", name="吡虫啉"),
                pesticide_item(knowledge_id="K-2002", name="代森锰锌"),
            ]
        ),
    )

    assert _run(ctx) == []


def test_empty_pesticide_items_pass() -> None:
    """正例 2：`pesticide_items = []` 是合法状态（候选为空时由转人工流程接住），返回空列表。"""
    ctx = gate_context(prescription=prescription(items=[]))

    assert _run(ctx) == []


# ---- 反例 ----


def test_unknown_knowledge_id_blocked() -> None:
    """反例 1：`knowledge_id` 不在库中，属"模型越界"，字段路径带该 id。"""
    ctx = gate_context(
        knowledge=FakeKnowledge({}),  # 空库：任何 id 都查不到
        prescription=prescription(items=[pesticide_item(knowledge_id="K-9999", name="某除草剂")]),
    )

    (violation,) = _run(ctx)

    assert violation.rule_id == GateRuleId.WHITELIST.value
    assert violation.severity == "block"
    assert violation.field == "pesticide_items[K-9999]"
    assert "不在已核准知识库中" in violation.message


def test_suspended_entry_blocked() -> None:
    """反例 2：条目存在但被暂停使用，属"知识库状态变更"，消息带出实际状态。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", status="suspended")}
    )
    ctx = gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "pesticide_items[K-1001]"
    assert "当前状态为 suspended" in violation.message


def test_name_mismatch_blocked() -> None:
    """反例 3：条目已核准但处方把药剂名改写了，这是"模型改写事实"的最危险形态。"""
    knowledge = FakeKnowledge({"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉")})
    ctx = gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫灵")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field.endswith(".name")
    assert "药剂名与知识库不一致" in violation.message


def test_deprecated_entry_blocked() -> None:
    """`deprecated` 保留用于历史追溯、不可用于新处方，因此本规则同样拦截。"""
    knowledge = FakeKnowledge(
        {"K-1001": FakeEntry(knowledge_id="K-1001", name="吡虫啉", status="deprecated")}
    )
    ctx = gate_context(
        knowledge=knowledge,
        prescription=prescription(items=[pesticide_item(knowledge_id="K-1001", name="吡虫啉")]),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert "当前状态为 deprecated" in violation.message


# ---- 规则元信息与装配 ----


def test_rule_metadata_and_registration() -> None:
    """等级 block、id 正确，且能按 id 从注册表实例化。"""
    assert PesticideWhitelistRule.severity == "block"
    assert PesticideWhitelistRule.rule_id == GateRuleId.WHITELIST.value

    (rule,) = build_rules([GateRuleId.WHITELIST.value])

    assert isinstance(rule, PesticideWhitelistRule)


def test_module_has_no_forbidden_imports() -> None:
    """L1：闸门不得依赖生成链路与服务层。"""
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
