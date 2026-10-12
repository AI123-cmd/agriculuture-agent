"""T6-08：`ObservabilityMasker` 的脱敏规则（含正例与不误伤）。

脱敏规则一旦误伤（把非敏感内容也掩掉）比漏掩更难排查：日志失去可读性却没有任何报错。
因此每个正则都配一条"不误伤"的反例。另有一条递归用例——嵌套结构漏掩是本组件唯一的
结构性缺陷点。
"""

from __future__ import annotations

import pytest

from app.observability.masker import ObservabilityMasker


@pytest.fixture()
def masker() -> ObservabilityMasker:
    return ObservabilityMasker()


# ---- 手机号 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("13800000000", "138****0000"),
        ("13912345678", "139****5678"),
        ("18612345678", "186****5678"),
    ],
)
def test_phone_is_masked_keeping_prefix_and_suffix(masker: ObservabilityMasker, raw: str, expected: str) -> None:
    """保留号段 + 前 3 位与末 4 位：同一农户的两条日志仍可关联。"""
    assert masker.mask_phone(raw) == expected


@pytest.mark.parametrize("raw", ["1360000", "12345", "1380000000", "138000000000"])
def test_non_phone_digit_strings_untouched(masker: ObservabilityMasker, raw: str) -> None:
    """不足或超过 11 位、或号段不合法的一律原样保留——不误伤。"""
    assert masker.mask_phone(raw) == raw


def test_phone_is_masked_inside_longer_text(masker: ObservabilityMasker) -> None:
    """手机号嵌在普通文本里也要清洗（异常堆栈常含这种文本）。"""
    assert masker.mask_phone("农户手机 13800000000 未接") == "农户手机 138****0000 未接"


def test_phone_substring_of_longer_digit_run_is_untouched(masker: ObservabilityMasker) -> None:
    """更长的数字串（如订单号）里恰好出现的 11 位片段不得被误伤。"""
    assert masker.mask_phone("2024013810000000") == "2024013810000000"


# ---- 设备 ID ----


def test_device_sn_is_masked_keeping_prefix(masker: ObservabilityMasker) -> None:
    """保留前 6 位便于定位批次，其余掩成固定长度的 `*`。"""
    assert masker.mask_device("AB12CD345678") == "AB12CD******"


@pytest.mark.parametrize("raw", ["A1B2C3", "ABC123", "short"])
def test_exactly_six_char_token_untouched(masker: ObservabilityMasker, raw: str) -> None:
    """恰 6 位不产生后缀 `******`：否则任何短标识都会被追加一串星号。"""
    assert masker.mask_device(raw) == raw


def test_device_mask_is_not_applied_to_plain_strings_globally(masker: ObservabilityMasker) -> None:
    """设备号规则只在声明字段上生效：摘要类长串（如 sha256）不得被误伤。"""
    digest = "a3f1c9d2e4b5" * 4
    assert masker.mask_value("input_digest", digest) == digest


# ---- 字段级：按名整体替换 ----


def test_sensitive_non_string_field_replaced_wholly(masker: ObservabilityMasker) -> None:
    """非字符串的敏感字段整体替换为 `***`（如 `raw_payload` 是 dict）。"""
    assert masker.mask_value("raw_payload", {"t1": 26.5}) == "***"


def test_device_id_field_is_masked(masker: ObservabilityMasker) -> None:
    assert masker.mask_value("device_sn", "AB12CD345678") == "AB12CD******"


def test_unknown_field_only_gets_phone_cleaning(masker: ObservabilityMasker) -> None:
    """非敏感字段里的手机号仍被清洗（手机号模式具体，全局安全）。"""
    assert masker.mask_value("note", "联系 13800000000") == "联系 138****0000"


# ---- 递归 ----


def test_nested_phone_is_masked(masker: ObservabilityMasker) -> None:
    """嵌套结构：`prescription.facts_provenance.phone` 必须被清洗（递归生效）。"""
    event = {"prescription": {"facts_provenance": {"phone": "13800000000"}}}
    result = masker.mask(event)
    assert result["prescription"]["facts_provenance"]["phone"] == "138****0000"


def test_sensitive_field_inside_list_is_masked(masker: ObservabilityMasker) -> None:
    """list 内的 dict 同样递归（日志里设备列表常见）。"""
    event = {"devices": [{"device_sn": "AB12CD345678"}, {"device_sn": "A1B2C3"}]}
    result = masker.mask(event)
    assert result["devices"][0]["device_sn"] == "AB12CD******"
    assert result["devices"][1]["device_sn"] == "A1B2C3"


def test_mask_does_not_mutate_input(masker: ObservabilityMasker) -> None:
    """返回新字典，不改动入参——处理器多次调用同一事件不应互相影响。"""
    event = {"phone": "13800000000"}
    masker.mask(event)
    assert event["phone"] == "13800000000"


# ---- 坐标开关 ----


def test_geo_kept_when_mask_geo_disabled() -> None:
    """`mask_geo=False`：坐标保留原值（本地调试需要真实坐标）。"""
    masker = ObservabilityMasker(mask_geo=False)
    assert masker.mask_value("latitude", 30.5) == 30.5
    assert masker.mask_value("longitude", "120.1") == "120.1"


def test_geo_masked_when_mask_geo_enabled() -> None:
    """`mask_geo=True`：坐标整体替换（生产必须开启）。"""
    masker = ObservabilityMasker(mask_geo=True)
    assert masker.mask_value("latitude", 30.5) == "***"
    assert masker.mask_value("longitude", "120.1") == "***"


def test_extra_fields_are_masked() -> None:
    """`extra_fields` 声明的字段纳入脱敏：字符串走手机号/设备号清洗，非字符串整体替换。"""
    masker = ObservabilityMasker(extra_fields=("id_card",))
    assert masker.mask_value("id_card", {"no": "330100"}) == "***"
    assert masker.mask_value("id_card", "AB12CD345678") == "AB12CD******"
