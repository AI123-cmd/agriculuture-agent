"""T2-11：判定契约的三条硬约束。

重点是"把握程度不是分数"这一条的结构性保证：契约里根本不存在可以放分数的字段，因此
展示层即使想渲染百分比也无从取值。
"""

import pytest
from pydantic import ValidationError

from app.agent.contracts import CONFIDENCE_DISPLAY, DiagnosisResult, dump_contract, load_contract


def _diagnosis(**overrides) -> dict:
    base = {
        "conclusion": "茶小绿叶蝉若虫危害",
        "confidence_level": "likely",
        "basis": ["叶片出现均匀失绿斑点，符合若虫刺吸特征"],
        "uncertainty_source": [],
        "excluded": ["茶炭疽病"],
        "terminology_profile": "tea",
        "needs_more_info": [],
        "out_of_scope": False,
        "boundary_reason": None,
    }
    base.update(overrides)
    return base


def test_valid_diagnosis_passes() -> None:
    result = DiagnosisResult(**_diagnosis())
    assert result.confidence_level == "likely"
    assert result.out_of_scope is False
    assert result.boundary_reason is None


def test_empty_basis_raises() -> None:
    with pytest.raises(ValidationError, match="basis"):
        DiagnosisResult(**_diagnosis(basis=[]))


def test_blank_basis_item_raises() -> None:
    """给了依据但某一条是空白，等价于没给——展示层会出现一个空条目。"""
    with pytest.raises(ValidationError, match="basis"):
        DiagnosisResult(**_diagnosis(basis=["叶片失绿", "   "]))


def test_out_of_scope_without_reason_raises() -> None:
    with pytest.raises(ValidationError, match="boundary_reason"):
        DiagnosisResult(**_diagnosis(out_of_scope=True))


def test_out_of_scope_with_reason_passes() -> None:
    result = DiagnosisResult(**_diagnosis(out_of_scope=True, boundary_reason="涉及土壤重金属检测，超出本流程能力"))
    assert result.out_of_scope is True


def test_uncertain_without_source_or_gap_raises() -> None:
    """不确定是合法结论，但不能只给一句"不确定"——必须说明不确定在哪或还缺什么。"""
    with pytest.raises(ValidationError, match="uncertain"):
        DiagnosisResult(**_diagnosis(confidence_level="uncertain", uncertainty_source=[], needs_more_info=[]))


def test_uncertain_with_source_passes() -> None:
    result = DiagnosisResult(**_diagnosis(confidence_level="uncertain", uncertainty_source=["未检索到支撑条目"]))
    assert result.confidence_level == "uncertain"


def test_uncertain_with_needs_more_info_passes() -> None:
    result = DiagnosisResult(**_diagnosis(confidence_level="uncertain", needs_more_info=["叶片背面近照"]))
    assert result.needs_more_info == ["叶片背面近照"]


def test_likely_may_omit_uncertainty_fields() -> None:
    """非 uncertain 时两者皆空是合法的：确定结论不需要凑一份不确定性说明。"""
    result = DiagnosisResult(**_diagnosis(confidence_level="preliminary", uncertainty_source=[], needs_more_info=[]))
    assert result.uncertainty_source == []


def test_no_score_like_fields_in_contract() -> None:
    """技术方案 5.3.4 硬约束：把握程度是枚举，不是数值。

    这条断言是结构性的——即使将来有人新增字段，只要名字里带分数语义就会在这里失败，
    从而强制该分数改放到 state 里（如 T2-19 的 `internal_confidence`）。
    """
    forbidden = ("score", "percent", "confidence_score")
    offenders = [n for n in DiagnosisResult.model_fields if any(f in n for f in forbidden)]
    assert offenders == []


def test_fields_match_spec() -> None:
    assert set(DiagnosisResult.model_fields) == {
        "conclusion",
        "confidence_level",
        "basis",
        "uncertainty_source",
        "excluded",
        "terminology_profile",
        "needs_more_info",
        "out_of_scope",
        "boundary_reason",
    }


def test_confidence_display_covers_literal_fully() -> None:
    assert CONFIDENCE_DISPLAY == {
        "likely": "很可能是",
        "preliminary": "初步判断",
        "uncertain": "不确定",
    }


@pytest.mark.parametrize(
    "level,display",
    [("likely", "很可能是"), ("preliminary", "初步判断"), ("uncertain", "不确定")],
)
def test_confidence_display_values(level: str, display: str) -> None:
    assert CONFIDENCE_DISPLAY[level] == display


def test_confidence_display_matches_literal_options() -> None:
    """映射表的键集合必须与 `Literal` 的取值集合一致：多一个键说明有档位已废弃，少一个
    键会在展示层以 `KeyError` 的形式暴露。"""
    literal_values = set(DiagnosisResult.model_fields["confidence_level"].annotation.__args__)
    assert set(CONFIDENCE_DISPLAY) == literal_values


def test_unknown_confidence_level_raises() -> None:
    with pytest.raises(ValidationError):
        DiagnosisResult(**_diagnosis(confidence_level="fairly_likely"))


def test_numeric_confidence_level_raises() -> None:
    """模型很容易输出 `0.85` 这种形态，必须在构造时被拦住。"""
    with pytest.raises(ValidationError):
        DiagnosisResult(**_diagnosis(confidence_level=0.85))


def test_excluded_may_be_empty_because_config_decides() -> None:
    """契约允许空排除项：只有存在易混淆项的流程才要求非空，而"是否有易混淆项"需要读流程
    配置，由 T2-19 的判定智能体做业务校验。"""
    result = DiagnosisResult(**_diagnosis(excluded=[]))
    assert result.excluded == []


def test_extra_field_raises() -> None:
    with pytest.raises(ValidationError):
        DiagnosisResult(**_diagnosis(score=0.85))


def test_json_schema_generates() -> None:
    schema = DiagnosisResult.model_json_schema()
    assert "confidence_level" in schema["properties"]
    assert "boundary_reason" in schema["properties"]


def test_roundtrip_through_state_payload() -> None:
    original = DiagnosisResult(**_diagnosis(confidence_level="uncertain", needs_more_info=["近照"]))
    payload = dump_contract(original)
    assert payload["confidence_level"] == "uncertain"
    assert load_contract(DiagnosisResult, payload) == original
