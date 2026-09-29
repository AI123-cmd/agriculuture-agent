"""T2-42：`R-REDLINE` 流程专属红线。

禁令措辞随流程与季节变化，写进 YAML 才能改配置即生效。规则读 `blocking_labels()` 做一次
文本扫描，覆盖禁用品种与禁用短语，并在三个位置定位——展示文本、动作文本、`detail_terms`
改写结果。只扫展示文本会漏掉后两类。
"""

from pathlib import Path

from app.gate.contracts import GateAction, GatePayload
from app.gate.engine import GateEngine
from app.gate.enums import GateRuleId, ProcessId
from app.gate.rules.base import build_rules
from app.gate.rules.process_redline import ProcessRedlineRule
from tests.gate.fixtures.factories import gate_context, pesticide_item, prescription
from tests.gate.fixtures.fake_knowledge import (
    loaded_redlines,
    make_redline,
    redlines_from,
    unavailable_redlines,
)

_RULE_MODULE = Path(__file__).resolve().parents[3] / "app" / "gate" / "rules" / "process_redline.py"


def _run(ctx):
    return ProcessRedlineRule().check(ctx)


# ---- 正例 ----


def test_text_without_forbidden_label_passes() -> None:
    """正例 1：红线含禁用品种"乐果"，文本不含该措辞。"""
    ctx = gate_context(
        redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"]),
        payload=GatePayload(rendered_text="建议在晴天傍晚喷施低毒杀虫剂。"),
    )

    assert _run(ctx) == []


def test_no_blocking_labels_passes() -> None:
    """正例 2：红线只有 warn 级，`blocking_labels()` 为空，规则直接返回空列表。"""
    warn_only = redlines_from(
        ProcessId.TEA,
        make_redline(ProcessId.TEA, redline_id="TEA-09", desc="提示", severity="warn", forbidden=["乐果"]),
    )
    ctx = gate_context(redlines=warn_only, payload=GatePayload(rendered_text="可选用乐果防治"))

    assert ctx.redlines.blocking_labels() == []
    assert _run(ctx) == []


# ---- 反例 ----


def test_forbidden_in_rendered_text_blocked() -> None:
    """反例 1：展示文本含禁用品种，`field` 指向展示文本，message 带出红线 id。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"], redline_id="TEA-02", desc="禁用品种"),
        payload=GatePayload(rendered_text="可选用乐果防治茶树小绿叶蝉。"),
    )

    (violation,) = _run(ctx)

    assert violation.severity == "block"
    assert violation.field == "gate_payload.rendered_text"
    assert "乐果" in violation.message
    assert "TEA-02" in violation.message


def test_forbidden_phrase_in_action_blocked() -> None:
    """反例 2：禁用短语在 `actions[0].cautions` 而展示文本不含，`field` 指向该动作。"""
    action = GateAction(
        order=1,
        action="喷施药剂",
        timing="晴天",
        method="叶面喷雾",
        cautions=["无需安全间隔期"],
        stop_conditions=[],
    )
    ctx = gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(
            ProcessId.TEA,
            forbidden_phrases=["无需安全间隔期"],
            redline_id="TEA-03",
            desc="禁用短语",
        ),
        payload=GatePayload(rendered_text="建议晴天喷施。"),
        prescription=prescription(items=[pesticide_item()], actions=[action]),
    )

    (violation,) = _run(ctx)

    assert violation.field == "actions[0]"
    assert "无需安全间隔期" in violation.message


def test_forbidden_in_detail_terms_blocked() -> None:
    """反例 3：`detail_terms` 的改写值含禁用品种名，`field` 以 `detail_terms[` 开头。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"], redline_id="TEA-02"),
        payload=GatePayload(rendered_text="建议晴天喷施。"),
        prescription=prescription(
            items=[pesticide_item()],
            detail_terms={"高效药剂": "乐果"},
        ),
    )

    (violation,) = _run(ctx)

    assert violation.field.startswith("detail_terms[")
    assert "乐果" in violation.message


# ---- 归一、空标签与归属 ----


def test_fullwidth_and_space_insertion_still_match() -> None:
    """全角/半角与空格插入经归一后仍能命中（`乐 果` vs `乐果`）。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(ProcessId.TEA, forbidden=["乐果"], redline_id="TEA-02"),
        payload=GatePayload(rendered_text="可选用乐 果防治。"),
    )

    (violation,) = _run(ctx)

    assert "乐果" in violation.message


def test_empty_label_does_not_match_everything() -> None:
    """空字符串标签不得造成全量拦截：配置书写错误不应等于"全部禁用"。"""
    ctx = gate_context(
        process_id=ProcessId.TEA,
        redlines=loaded_redlines(ProcessId.TEA, forbidden=[""], redline_id="TEA-02"),
        payload=GatePayload(rendered_text="这是一段普通建议。"),
    )

    assert _run(ctx) == []


def test_rule_ref_not_attributed_when_redline_is_warn_only() -> None:
    """`_rule_ref` 只在红线归属内反查：不计入 blocking_labels 的红线不会被误归属。"""
    mixed = redlines_from(
        ProcessId.TEA,
        make_redline(ProcessId.TEA, redline_id="TEA-02", desc="禁用品种", severity="block", forbidden=["乐果"]),
        make_redline(ProcessId.TEA, redline_id="TEA-09", desc="提示项", severity="warn", forbidden=["吡虫啉"]),
    )
    ctx = gate_context(process_id=ProcessId.TEA, redlines=mixed, payload=GatePayload(rendered_text="可选用乐果防治"))

    (violation,) = _run(ctx)

    assert "TEA-02" in violation.message
    assert "TEA-09" not in violation.message


# ---- 与引擎短路的边界 ----


def test_not_executed_when_redlines_unloaded() -> None:
    """`loaded=False` 时引擎短路，`checked_rules` 不含 `R-REDLINE`。"""
    ctx = gate_context(redlines=unavailable_redlines(ProcessId.TEA))

    result = GateEngine().evaluate(ctx)

    assert GateRuleId.REDLINE.value not in result.checked_rules
    assert result.checked_rules == [GateRuleId.REDLINE_LOAD.value]


# ---- 装配 ----


def test_rule_metadata_and_registration() -> None:
    assert ProcessRedlineRule.rule_id == GateRuleId.REDLINE.value
    assert ProcessRedlineRule.severity == "block"

    (rule,) = build_rules([GateRuleId.REDLINE.value])

    assert isinstance(rule, ProcessRedlineRule)


def test_blocking_redline_enters_blocking_labels() -> None:
    """block 级红线的禁用项确实进入 `blocking_labels()`。"""
    redlines = loaded_redlines(ProcessId.TEA, forbidden=["乐果"], redline_id="TEA-02")

    assert "乐果" in redlines.blocking_labels()
    assert "R-REDLINE" in redlines.declared_rule_ids()


def test_module_has_no_forbidden_imports() -> None:
    source = _RULE_MODULE.read_text(encoding="utf-8")

    assert "app.agent" not in source
    assert "app.services" not in source
