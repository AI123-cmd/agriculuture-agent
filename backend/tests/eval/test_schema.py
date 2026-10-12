"""离线评测集 Schema 单测（T6-01）。

覆盖三类：Schema 逐字对齐（改名/增删字段会当场失败）、引用的枚举确实存在、
`load_cases` 的两条边界（空目录返回空表、非法 JSON 抛带上下文的明确错误）。
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.state import ProcessId
from tests.eval.report import compare_consistency
from tests.eval.schema import (
    EvalCase,
    EvalCaseLoadError,
    EvalExpectation,
    EvalResult,
    load_cases,
)


def _case(**overrides) -> dict:
    """一道最小合法题，供各用例按需覆写字段。"""
    payload = {
        "case_id": "tea_001",
        "process_id": "tea",
        "scene": "茶园高温干旱",
        "inputs": {"message": "我的茶树叶子发黄"},
        "expect": {
            "expect_process": "tea",
            "allowed_conclusions": ["茶小绿叶蝉"],
        },
    }
    payload.update(overrides)
    return payload


def test_schema_fields_are_exact():
    """`EvalExpectation` / `EvalCase` 的字段集合与技术方案 14.1 一致（改名或增删即失败）。"""
    assert set(EvalCase.model_fields) == {"case_id", "process_id", "scene", "inputs", "expect"}
    assert set(EvalExpectation.model_fields) == {
        "expect_process",
        "must_ask_slots",
        "allowed_conclusions",
        "expect_gate_rules",
        "forbidden_phrases",
        "allowed_sources",
    }


def test_expect_process_rejects_free_string():
    """`expect_process` 必须落在 `ProcessId` 内，自由字符串（含首尾空格）抛 `ValidationError`。"""
    with pytest.raises(ValidationError):
        EvalExpectation(expect_process="tea ", allowed_conclusions=["x"])


def test_allowed_conclusions_required():
    """`allowed_conclusions` 无默认值：不给允许集合等于放弃断言，构造必须失败。"""
    with pytest.raises(ValidationError):
        EvalExpectation(expect_process="tea")


def test_allowed_sources_default():
    """`allowed_sources` 缺省为三类用户可见依据，不含 `model_inference`。"""
    expectation = EvalExpectation(expect_process="tea", allowed_conclusions=["x"])
    assert expectation.allowed_sources == ["process_config", "knowledge_base", "env_reading"]


def test_process_id_enum_is_closed():
    """评测只认七条流程：`tobacco` 不是合法流程（枚举封闭，技术方案 5.2）。"""
    with pytest.raises(ValidationError):
        EvalCase.model_validate(_case(process_id="tobacco"))


def test_load_cases_empty_dir_returns_empty(tmp_path: Path):
    """`cases/` 为空时返回空列表且不报错（T6-01 交付时内容尚未填充）。"""
    assert load_cases(ProcessId.TEA, cases_dir=tmp_path) == []


def test_load_cases_missing_file_returns_empty(tmp_path: Path):
    """文件不存在同样返回空列表——未填写的流程不构成失败。"""
    assert load_cases(ProcessId.PIG, cases_dir=tmp_path) == []


def test_load_cases_invalid_json_raises_with_context(tmp_path: Path):
    """非法 JSON 抛 `EvalCaseLoadError`，消息含文件名，不是裸 `JSONDecodeError`。"""
    (tmp_path / "tea.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(EvalCaseLoadError) as excinfo:
        load_cases(ProcessId.TEA, cases_dir=tmp_path)
    assert "tea.json" in str(excinfo.value)


def test_load_cases_invalid_case_raises_with_case_id(tmp_path: Path):
    """某道题不满足 Schema 时，错误消息带 `case_id`，便于维护者定位到具体某题。"""
    payload = {"cases": [_case(case_id="tea_007", expect={"expect_process": "not_a_process"})]}
    (tmp_path / "tea.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvalCaseLoadError) as excinfo:
        load_cases(ProcessId.TEA, cases_dir=tmp_path)
    assert "tea_007" in str(excinfo.value)


def test_load_cases_roundtrip(tmp_path: Path):
    """合法文件正常加载，`expect_process` 解析为 `ProcessId`。"""
    (tmp_path / "tea.json").write_text(json.dumps({"cases": [_case()]}, ensure_ascii=False), encoding="utf-8")
    cases = load_cases(ProcessId.TEA, cases_dir=tmp_path)
    assert len(cases) == 1
    assert cases[0].expect.expect_process is ProcessId.TEA


def test_eval_result_uses_doc_field_names():
    """`EvalResult` 用文档口径的 `conclusion_key` / `gate.rules_hit`，不是生产字段名。"""
    from tests.eval.schema import EvalGate

    assert "conclusion_key" in EvalResult.model_fields
    assert "conclusion" not in EvalResult.model_fields
    assert "rules_hit" in EvalGate.model_fields
    assert "checked_rules" not in EvalGate.model_fields


def test_compare_consistency_first_run_passes():
    """无历史报告时返回"放行"并提示首次评测。"""
    current = {"overall": {"consistency": 0.95}, "processes": {}}
    decision, note = compare_consistency(current, None)
    assert decision == "pass"
    assert "首次评测" in note


def test_compare_consistency_blocks_on_absolute_threshold():
    """整体一致率低于绝对阈值即阻断。"""
    current = {"overall": {"consistency": 0.5}, "processes": {}}
    decision, _ = compare_consistency(current, None, pass_rate_threshold=0.9)
    assert decision == "blocked"


def test_compare_consistency_blocks_on_regression():
    """按流程下降超过阈值即阻断，即使整体一致率仍达标。"""
    previous = {"overall": {"consistency": 0.99}, "processes": {"tea": {"consistency": 0.99}}}
    current = {"overall": {"consistency": 0.95}, "processes": {"tea": {"consistency": 0.90}}}
    decision, note = compare_consistency(current, previous)
    assert decision == "blocked"
    assert "tea" in note
