"""T2-06：红线领域模型与完整性校验。

四类反例对应四种「红线看起来配了但其实不生效」的写法：整段为空、id 重复（后者
覆盖前者）、引用了不存在的规则 id（永远不会被执行）、`data` 里写了不会被读取的键。
其中第三类最危险——它不会报错、不会命中，只在事后复盘时才发现这条红线是装饰。
"""

import pytest

from app.agent.state import ProcessId, ViolationSeverity
from app.process_config.loader import load_all_configs, load_process_config
from app.process_config.models import ProcessConfig, RedlineSpec
from app.process_config.redlines import LoadedRedlines, RedlineValidator, build_availability
from app.process_config.rule_ids import GateRuleId, parse_rule_id


@pytest.fixture(scope="module")
def validator() -> RedlineValidator:
    return RedlineValidator()


@pytest.fixture(scope="module")
def configs() -> dict[ProcessId, ProcessConfig]:
    return load_all_configs()


def _with_redlines(config: ProcessConfig, redlines: list[RedlineSpec]) -> ProcessConfig:
    """替换红线列表。`frozen=True` 只挡字段赋值，`model_copy(update=...)` 是允许的。"""
    return config.model_copy(update={"redlines": redlines})


def _spec(redline_id: str, rule: str, severity: str = "block", data: dict | None = None) -> RedlineSpec:
    return RedlineSpec(id=redline_id, desc="d", rule=rule, severity=severity, data=data or {})


def test_all_seven_flows_loaded(configs, validator) -> None:
    """对 T2-04 产出的七份配置运行 build_availability，七条流程 loaded 均为 True。"""
    manifest = build_availability(validator, configs)
    assert set(manifest) == set(ProcessId)
    for pid, result in manifest.items():
        assert result.loaded is True, f"{pid.value}: {result.unavailable_reason}"


def test_empty_redlines_marks_unavailable(validator) -> None:
    config = _with_redlines(load_process_config(ProcessId.TEA), [])
    result = validator.validate(config)
    assert result.loaded is False
    assert "redlines 为空" in (result.unavailable_reason or "")
    assert result.redlines == ()


def test_duplicate_redline_id_marks_unavailable(configs, validator) -> None:
    config = configs[ProcessId.TEA]
    redlines = list(config.redlines) + [_spec("TEA-01", "R-REDLINE")]
    result = validator.validate(_with_redlines(config, redlines))
    assert result.loaded is False
    assert "重复" in (result.unavailable_reason or "")


def test_unknown_rule_id_marks_unavailable(configs, validator) -> None:
    """引用不存在的规则 id 的红线永远不会被执行，必须在加载期拦住。"""
    config = configs[ProcessId.TEA]
    redlines = [
        _spec("TEA-02", "R-NOT-EXIST"),
        *[r for r in config.redlines if r.id != "TEA-02"],
    ]
    result = validator.validate(_with_redlines(config, redlines))
    assert result.loaded is False
    assert "R-NOT-EXIST" in (result.unavailable_reason or "")


def test_payload_key_not_allowed_marks_unavailable(configs, validator) -> None:
    """`data` 里写了不会被读取的键，等于给了「已配置额外约束」的错觉。"""
    config = configs[ProcessId.TEA]
    redlines = [
        _spec("TEA-02", "R-REDLINE", data={"nonexistent_key": 1}),
        *[r for r in config.redlines if r.id != "TEA-02"],
    ]
    result = validator.validate(_with_redlines(config, redlines))
    assert result.loaded is False
    assert "nonexistent_key" in (result.unavailable_reason or "")


def test_payload_on_rule_that_takes_none_marks_unavailable(configs, validator) -> None:
    """`R-WHITELIST` 的判据完全来自知识库，配置侧补载荷无从执行。"""
    config = configs[ProcessId.TEA]
    redlines = list(config.redlines) + [_spec("TEA-99", "R-WHITELIST", data={"limit": 1})]
    result = validator.validate(_with_redlines(config, redlines))
    assert result.loaded is False
    assert "不接受 data" in (result.unavailable_reason or "")


def test_empty_rule_token_marks_unavailable(configs, validator) -> None:
    """逗号分隔留下空项（如 `R-INTERVAL,`）属于书写错误。"""
    config = configs[ProcessId.TEA]
    redlines = list(config.redlines) + [_spec("TEA-98", "R-INTERVAL,")]
    result = validator.validate(_with_redlines(config, redlines))
    assert result.loaded is False
    assert "空项" in (result.unavailable_reason or "")


def test_unavailable_is_safely_usable() -> None:
    """`unavailable` 的结果不需要判空即可安全调用 `blocking_labels()`。"""
    result = LoadedRedlines.unavailable(ProcessId.TEA, 3, "配置未加载")
    assert result.redlines == ()
    assert result.loaded is False
    assert result.blocking_labels() == []
    assert result.declared_rule_ids() == []
    assert result.unavailable_reason == "配置未加载"
    assert result.version == 3


def test_tea_declared_rule_ids_cover_expected_set(configs, validator) -> None:
    """茶流程声明的规则 id 应覆盖五条：登记作物、红线、间隔期、每季次数、地区。"""
    result = validator.validate(configs[ProcessId.TEA])
    assert result.loaded is True
    declared = set(result.declared_rule_ids())
    assert {"R-REGISTERED-CROP", "R-REDLINE", "R-INTERVAL", "R-MAX-USE", "R-REGION"} <= declared


def test_redline_rule_ids_expanded_from_comma(configs, validator) -> None:
    """`R-INTERVAL,R-MAX-USE` 必须展开成两个规则 id，而不是一个含逗号的字符串。"""
    result = validator.validate(configs[ProcessId.TEA])
    tea03 = next(item for item in result.redlines if item.id == "TEA-03")
    assert tea03.rule_ids == (GateRuleId.INTERVAL, GateRuleId.MAX_USE)


def test_blocking_labels_merges_forbidden_and_phrases(configs, validator) -> None:
    """`forbidden`（禁用品种）与 `forbidden_phrases`（禁用短语）统一暴露给闸门。"""
    result = validator.validate(configs[ProcessId.TEA])
    tea02 = next(item for item in result.redlines if item.id == "TEA-02")
    assert "乙酰甲胺磷" in tea02.labels()

    merged = validator.validate(configs[ProcessId.WEED]).blocking_labels()
    assert any("直接打这种药" in label for label in merged)
    assert any("后茬可种任意作物" in label for label in merged)


def test_is_blocking_reflects_severity(configs, validator) -> None:
    result = validator.validate(configs[ProcessId.TEA])
    tea05 = next(item for item in result.redlines if item.id == "TEA-05")
    assert tea05.severity is ViolationSeverity.WARN
    assert tea05.is_blocking is False
    # TEA-05 是唯一的 warn 级红线且不带 payload，因此不应贡献任何 block 文本
    assert tea05.labels() == []


def test_availability_includes_missing_flows(validator) -> None:
    """缺项的流程必须显式出现在清单里并标记不可用，而不是静默消失。"""
    manifest = build_availability(validator, {})
    assert set(manifest) == set(ProcessId)
    for pid, result in manifest.items():
        assert result.loaded is False
        assert result.unavailable_reason == "配置未加载"


def test_availability_carries_version(configs, validator) -> None:
    manifest = build_availability(validator, configs)
    for pid, result in manifest.items():
        assert result.process_id is pid
        assert result.version == configs[pid].version


def test_parse_rule_id_returns_none_for_unknown() -> None:
    assert parse_rule_id("R-INTERVAL") is GateRuleId.INTERVAL
    assert parse_rule_id("R-NOT-EXIST") is None


def test_rule_payload_spec_covers_all_rule_ids() -> None:
    """规格表必须覆盖全部规则 id，漏一条会让该规则的红线载荷变成「无限制」。"""
    from app.process_config.rule_ids import RULE_PAYLOAD_SPEC

    assert set(RULE_PAYLOAD_SPEC) == set(GateRuleId)


def test_valid_config_with_payload_passes(configs, validator) -> None:
    config = configs[ProcessId.PIG]
    result = validator.validate(config)
    assert result.loaded is True
    pig01 = next(item for item in result.redlines if item.id == "PIG-01")
    assert pig01.payload["notice"]


def test_rapeseed_redline_loaded_as_block(configs, validator) -> None:
    result = validator.validate(configs[ProcessId.RAPESEED])
    assert result.loaded is True
    rape01 = next(item for item in result.redlines if item.id == "RAPE-01")
    assert rape01.is_blocking is True
    assert rape01.rule_ids == (GateRuleId.REDLINE,)
