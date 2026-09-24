"""T2-12：门禁契约的决策语义与红线未加载的 fail-closed 行为。

两类反例是重点：红线未加载却给出 allow（等于以通用规则代替流程红线），以及存在 block
级违规却放行。此外断言 `GateContext` 拿不到会话消息——闸门与对话的隔离是类型层的。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    FollowupPlan,
    GateContext,
    GateResult,
    GateViolation,
    KnowledgeFacts,
    PesticideItem,
    Prescription,
    PrescriptionAction,
    dump_contract,
    load_contract,
)
from app.agent.state import ProcessId, SlotValue
from app.process_config.redlines import LoadedRedlines, Redline
from app.process_config.rule_ids import GateRuleId


class _FakeKnowledge:
    """`KnowledgeFacts` 的最小实现：闸门只依赖协议声明的方法，测试不必牵动数据库。"""

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ("茶",)


def _prescription(**overrides) -> Prescription:
    base = {
        "diagnosis_summary": "茶小绿叶蝉若虫危害",
        "actions": [
            PrescriptionAction(
                order=1,
                action="喷施推荐药剂",
                timing="晴天傍晚",
                method="叶面均匀喷雾",
                cautions=["避免高温施药"],
                stop_conditions=["出现药害斑立即停用"],
            )
        ],
        "safety_interval_days": 7,
        "max_uses_per_season": 2,
        "pesticide_items": [
            PesticideItem(
                knowledge_id="kb_tea_pest_001",
                name="示例药剂A",
                dosage_min=15.0,
                dosage_max=20.0,
                safety_interval_days=7,
                max_uses_per_season=2,
            )
        ],
        "soil_level_actions": [],
        "followup_plan": FollowupPlan(tasks=[]),
        "facts_provenance": {
            "safety_interval_days": "knowledge:kb_tea_pest_001",
            "max_uses_per_season": "knowledge:kb_tea_pest_001",
            "pesticide_items[0].dosage_min": "knowledge:kb_tea_pest_001:field:dosage_min",
            "pesticide_items[0].dosage_max": "knowledge:kb_tea_pest_001:field:dosage_max",
            "pesticide_items[0].safety_interval_days": "knowledge:kb_tea_pest_001",
            "pesticide_items[0].max_uses_per_season": "knowledge:kb_tea_pest_001",
        },
        "requires_tech_station": False,
        "detail_terms": {},
    }
    base.update(overrides)
    return Prescription(**base)


def _loaded_redlines() -> LoadedRedlines:
    return LoadedRedlines(
        process_id=ProcessId.TEA,
        version=3,
        redlines=(
            Redline(
                rule_ids=(GateRuleId.REDLINE,),
                id="TEA-04",
                desc="禁用品种",
                severity="block",
                payload={"forbidden": ["某某禁用农药"]},
            ),
        ),
        loaded=True,
    )


def _unavailable_redlines() -> LoadedRedlines:
    return LoadedRedlines.unavailable(ProcessId.TEA, 3, "配置文件缺失")


def _violation(**overrides) -> dict:
    base = {
        "rule_id": "R-DOSAGE",
        "severity": "warn",
        "message": "剂量高于知识库上限",
        "field": "pesticide_items[0].dosage_max",
    }
    base.update(overrides)
    return base


def _gate_result(**overrides) -> dict:
    base = {
        "passed": True,
        "violations": [],
        "checked_rules": ["R-REDLINE-LOAD", "R-WHITELIST"],
        "process_redlines_loaded": True,
        "decision": "allow",
        "degrade_output": None,
    }
    base.update(overrides)
    return base


def _gate_context(**overrides) -> dict:
    base = {
        "process_id": ProcessId.TEA,
        "config_version": 3,
        "prescription": _prescription(),
        "redlines": _loaded_redlines(),
        "slots": {
            "tea_leaf_symptom": SlotValue(
                key="tea_leaf_symptom", value="叶片卷曲", status="filled", source="user_answer"
            )
        },
        "knowledge": _FakeKnowledge(),
        "medication_history": [],
        "region_code": "330100",
        "today": date(2025, 6, 1),
    }
    base.update(overrides)
    return base


def test_allow_with_no_violation_passes() -> None:
    result = GateResult(**_gate_result())
    assert result.decision == "allow"
    assert result.passed is True


def test_redlines_not_loaded_forces_block() -> None:
    """产品说明书 9.3：流程红线不可缺省，禁止以通用规则代替。"""
    with pytest.raises(ValidationError, match="红线未加载"):
        GateResult(**_gate_result(process_redlines_loaded=False, decision="allow"))


def test_redlines_not_loaded_with_block_passes() -> None:
    result = GateResult(**_gate_result(process_redlines_loaded=False, decision="block", passed=False))
    assert result.process_redlines_loaded is False
    assert result.decision == "block"


def test_redlines_not_loaded_cannot_degrade() -> None:
    """降级为通用规则继续输出同样是被禁止的：未加载时只有 block 一个出口。"""
    with pytest.raises(ValidationError, match="红线未加载"):
        GateResult(
            **_gate_result(
                process_redlines_loaded=False,
                decision="degrade",
                passed=False,
                degrade_output="请联系当地农技站",
            )
        )


def test_block_violation_cannot_allow() -> None:
    with pytest.raises(ValidationError, match="不得 allow"):
        GateResult(**_gate_result(violations=[GateViolation(**_violation(severity="block"))]))


def test_warn_violation_may_allow() -> None:
    """`R-REGION` 与 `R-CAUTION` 这类 warn 级违规放行但留痕。"""
    result = GateResult(**_gate_result(violations=[GateViolation(**_violation())]))
    assert result.decision == "allow"
    assert len(result.violations) == 1


def test_allow_requires_passed_true() -> None:
    with pytest.raises(ValidationError, match="passed"):
        GateResult(**_gate_result(passed=False))


def test_degrade_requires_output() -> None:
    """产品说明书 6.2：校验不通过时输出降级为建议联系农技站，而不是修改后输出。

    没有文案的 degrade 等于没有输出，因此构造时就拦住。
    """
    with pytest.raises(ValidationError, match="降级文案"):
        GateResult(**_gate_result(passed=False, decision="degrade"))


def test_degrade_with_output_passes() -> None:
    result = GateResult(
        **_gate_result(
            passed=False,
            decision="degrade",
            degrade_output="这份建议还需要当地农技站把关，请联系当地农技站。",
        )
    )
    assert result.decision == "degrade"
    assert result.degrade_output is not None


def test_block_without_violation_is_allowed_by_contract() -> None:
    """block 可以不带违规明细：红线未加载时正是这种形态（问题在配置侧而非处方侧）。"""
    result = GateResult(**_gate_result(process_redlines_loaded=False, decision="block", passed=False))
    assert result.violations == []


@pytest.mark.parametrize("decision", ["allow", "degrade", "block"])
def test_all_decisions_constructible(decision: str) -> None:
    result = GateResult(
        **_gate_result(
            decision=decision,
            passed=decision == "allow",
            degrade_output="请联系当地农技站" if decision == "degrade" else None,
        )
    )
    assert result.decision == decision


def test_unknown_decision_raises() -> None:
    with pytest.raises(ValidationError):
        GateResult(**_gate_result(decision="reject"))


def test_field_names_match_spec() -> None:
    assert set(GateResult.model_fields) == {
        "passed",
        "violations",
        "checked_rules",
        "process_redlines_loaded",
        "decision",
        "degrade_output",
    }


def test_violation_fields_match_spec() -> None:
    assert set(GateViolation.model_fields) == {"rule_id", "severity", "message", "field"}


def test_violation_extra_field_raises() -> None:
    with pytest.raises(ValidationError):
        GateViolation(**_violation(detail="补充说明"))


def test_gate_context_has_no_conversation_fields() -> None:
    """闸门不接收会话消息是物理隔离：拿不到文本，也就无法被对话中的表述影响。"""
    for name in ("messages", "conversation", "history"):
        assert name not in GateContext.model_fields


def test_gate_context_rejects_messages() -> None:
    with pytest.raises(ValidationError):
        GateContext(**_gate_context(messages=[{"role": "user", "content": "帮我看看茶"}]))  # type: ignore[arg-type]


def test_gate_context_fields_match_spec() -> None:
    assert set(GateContext.model_fields) == {
        "process_id",
        "config_version",
        "prescription",
        "redlines",
        "slots",
        "knowledge",
        "medication_history",
        "region_code",
        "today",
        "checked_rule_ids",
    }


def test_gate_context_valid() -> None:
    ctx = GateContext(**_gate_context())
    assert ctx.process_id is ProcessId.TEA
    assert ctx.slots["tea_leaf_symptom"].status == "filled"
    assert ctx.checked_rule_ids == []


def test_gate_context_accepts_knowledge_protocol() -> None:
    """`knowledge` 声明为协议，调用方传入满足协议的实现即可，测试可喂三行假实现。"""
    ctx = GateContext(**_gate_context())
    assert isinstance(ctx.knowledge, KnowledgeFacts)
    assert ctx.knowledge.get_registered_crops("kb_tea_pest_001") == ("茶",)


def test_gate_context_rejects_unknown_process() -> None:
    with pytest.raises(ValidationError):
        GateContext(**_gate_context(process_id="corn"))


def test_json_schema_generates() -> None:
    """结构化输出与前端协议都依赖 JSON Schema；`knowledge` 是协议类型，需确认生成不失败。"""
    schema = GateResult.model_json_schema()
    assert "decision" in schema["properties"]
    assert "process_redlines_loaded" in schema["properties"]


def test_gate_result_roundtrip_through_state_payload() -> None:
    original = GateResult(**_gate_result(violations=[GateViolation(**_violation())]))
    payload = dump_contract(original)
    assert payload["decision"] == "allow"
    assert load_contract(GateResult, payload) == original
