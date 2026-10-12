"""评测集自身校验（T6-02）。

评测题的断言是**子集断言**（`must_ask_slots ⊆ asked_slots`、`expect_gate_rules ⊆ rules_hit`
等），这类断言有一个隐蔽的失效方式：只要写了一个不存在的名字（错槽位名、错规则 id、
错来源名），子集关系会**恒成立**——评测于是"永远通过"，却一个真实问题都没测到。本文件
的作用就是把这类"静默恒真"在数据层拦住。

逐项校验（对应 T6-02 实现要点五）：

1. 文件级 `process_id` 与文件名一致；`case_id` 以流程前缀开头且**跨文件全局唯一**。
2. `must_ask_slots` 的每个槽位都在该流程 YAML 配置的槽位定义中存在。
3. `expect_gate_rules` 取值都在 8.2 的十二类规则清单内。
4. `allowed_sources` 取值都在 `SourceType` 内。
5. `process_id=general` 的题允许两种 `expect_process`（含反向题）；其余流程文件的
   `expect_process` 必须等于自身 `process_id`。
6. `allowed_conclusions` 非空且不含空串；`forbidden_phrases` 不含空串。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from app.agent.state import ProcessId
from app.gate.enums import GateRuleId
from app.process_config.loader import get_process_config
from tests.eval.schema import CASES_DIR, EvalCase, SourceType

# 反向题集中的流程前缀来自"本不该落 general"的具体流程，故 general 文件允许任意前缀。
_ANY_PREFIX = {"general"}

_GATE_RULE_IDS = frozenset(rule.value for rule in GateRuleId)
_SOURCE_TYPES = frozenset(get_args(SourceType))
_CASE_FILES = sorted(path for path in CASES_DIR.glob("*.json"))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_cases() -> list[tuple[Path, dict]]:
    """摊平所有文件里的原始题 dict，带上所属文件路径，便于失败信息定位。"""
    rows: list[tuple[Path, dict]] = []
    for path in _CASE_FILES:
        for row in _read(path).get("cases", []):
            rows.append((path, row))
    return rows


def test_seven_case_files_exist():
    """七条流程各一个文件，文件名与 `ProcessId` 一一对应。"""
    names = {path.name for path in _CASE_FILES}
    expected = {f"{pid.value}.json" for pid in ProcessId}
    assert names == expected, f"评测集文件与 ProcessId 不匹配：缺 {expected - names}，多 {names - expected}"


def test_file_process_id_matches_name():
    """文件级 `process_id` 必须与文件名一致（否则 `load_cases` 按名读取会读到别流程的题）。"""
    for path in _CASE_FILES:
        payload = _read(path)
        assert payload.get("process_id") == path.stem, f"{path.name}: process_id 与文件名不一致"


def test_case_id_prefix_and_global_uniqueness():
    """`case_id` 以流程前缀开头且跨文件唯一（重复 id 会让报告定位不到题）。"""
    seen: dict[str, Path] = {}
    for path, row in _all_cases():
        case_id = row.get("case_id")
        assert isinstance(case_id, str) and case_id, f"{path.name}: 缺 case_id"
        prefix = path.stem if path.stem in _ANY_PREFIX else path.stem
        assert case_id.startswith(f"{prefix}-"), f"{path.name}: case_id {case_id} 未以流程前缀开头"
        assert case_id not in seen, f"{case_id} 在 {seen.get(case_id)} 与 {path.name} 中重复"
        seen[case_id] = path


def test_must_ask_slots_exist_in_config():
    """`must_ask_slots` 必须取自该流程 YAML 的槽位定义，写错即恒真。"""
    for path, row in _all_cases():
        case_id = row.get("case_id")
        process_id = ProcessId(row.get("process_id"))
        declared = {spec.key for spec in get_process_config(process_id).required_slots}
        for slot in row.get("expect", {}).get("must_ask_slots", []):
            assert slot in declared, f"{path.name} / {case_id}: 槽位 {slot} 不存在于 {process_id.value} 配置"


def test_expect_gate_rules_are_declared():
    """`expect_gate_rules` 取值必须落在 8.2 的十二类规则清单内。"""
    for path, row in _all_cases():
        for rule_id in row.get("expect", {}).get("expect_gate_rules", []):
            assert rule_id in _GATE_RULE_IDS, f"{path.name} / {row.get('case_id')}: 未知规则 id {rule_id}"


def test_allowed_sources_are_declared():
    """`allowed_sources` 取值必须落在 `SourceType` 内。"""
    for path, row in _all_cases():
        for source in row.get("expect", {}).get("allowed_sources", []):
            assert source in _SOURCE_TYPES, f"{path.name} / {row.get('case_id')}: 未知来源 {source}"


def test_expect_process_consistency():
    """非 general 流程的 `expect_process` 必须等于自身；general 允许具体流程（反向题）。"""
    for path, row in _all_cases():
        process_id = row.get("process_id")
        expect_process = row.get("expect", {}).get("expect_process")
        if process_id == "general":
            assert expect_process in {pid.value for pid in ProcessId}, f"{path.name}: 非法 expect_process"
            continue
        assert expect_process == process_id, f"{path.name} / {row.get('case_id')}: expect_process 与流程不一致"


def test_conclusions_and_phrases_non_blank():
    """`allowed_conclusions` 非空且不含空串；`forbidden_phrases` 不含空串。"""
    for path, row in _all_cases():
        expect = row.get("expect", {})
        conclusions = expect.get("allowed_conclusions")
        assert conclusions, f"{path.name} / {row.get('case_id')}: allowed_conclusions 为空"
        assert all(isinstance(c, str) and c.strip() for c in conclusions), f"{path.name}: 结论含空串"
        assert all(isinstance(p, str) and p.strip() for p in expect.get("forbidden_phrases", [])), (
            f"{path.name}: forbidden_phrases 含空串"
        )


def test_cases_load_through_schema():
    """所有题都能通过 `EvalCase` 校验（Schema 与内容双向一致）。"""
    for path in _CASE_FILES:
        for row in _read(path).get("cases", []):
            EvalCase.model_validate(row)


def test_general_has_reverse_cases():
    """`general.json` 至少 6 题，其中反向题（`expect_process` 为具体流程）不少于 3 道。"""
    rows = _read(CASES_DIR / "general.json").get("cases", [])
    assert len(rows) >= 6, "general 评测集题目不足 6 道"
    reverse = [row for row in rows if row["expect"]["expect_process"] != "general"]
    assert len(reverse) >= 3, f"general 反向题不足 3 道（当前 {len(reverse)}）"


def test_scale_requirements():
    """规模下限：茶/辣椒各 ≥10，水稻 ≥8，pig 至少 1 道报告守卫题。"""
    assert len(_read(CASES_DIR / "tea.json")["cases"]) >= 10
    assert len(_read(CASES_DIR / "chili.json")["cases"]) >= 10
    assert len(_read(CASES_DIR / "rice.json")["cases"]) >= 8

    pig_rows = _read(CASES_DIR / "pig.json")["cases"]
    guard_rows = [
        row
        for row in pig_rows
        if "pig_report_required" in row["expect"]["allowed_conclusions"]
        or "R-PROVENANCE" in row["expect"].get("expect_gate_rules", [])
    ]
    assert guard_rows, "pig.json 缺少报告守卫用例（结论集合未体现不漏报）"


def test_weights_file_thresholds():
    """`weights.toml`：`general = 1.5` 且 `rapeseed < 1.0`。"""
    from tests.eval import runner

    weights = runner.load_weights()
    assert weights.get("general") == pytest.approx(1.5)
    assert weights.get("rapeseed", 1.0) < 1.0
