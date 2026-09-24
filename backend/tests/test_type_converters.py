"""T1-14：三类差异转换函数的纯函数单测。

覆盖任务文件逐条列出的验收点：`convert_json` 的 dict 返回与空值判定、`convert_bool`
的非布尔值报错（含列名）、`convert_datetime` 的时区偏移。全部用例不连数据库。
"""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.migrate.type_converters import (
    DEFAULT_SOURCE_TZ,
    convert_bool,
    convert_datetime,
    convert_json,
)

# ---- convert_json ----


def test_convert_json_bytes_returns_dict_not_str() -> None:
    """`convert_json(b'{"a":1}')` 返回 `{"a": 1}`（dict 而非 str）。

    这条是差异一的核心：直接把字符串塞进 JSONB，PG 会存成 JSON 字符串，
    读出来是 `"{\\"a\\":1}"` 而不是 `{"a": 1}`。
    """
    result = convert_json(b'{"a":1}')
    assert result == {"a": 1}
    assert isinstance(result, dict)


def test_convert_json_str_returns_object() -> None:
    """str 形态（PyMySQL 也可能返回 str）同样要解析成对象。"""
    assert convert_json("[1, 2, 3]") == [1, 2, 3]


@pytest.mark.parametrize("value", ["", "   ", "null", None, b"", b"null"])
def test_convert_json_empty_and_null_return_none(value) -> None:
    """`convert_json("")` 与 `convert_json("null")` 均返回 `None`，不是字符串 `"null"`。"""
    assert convert_json(value) is None


def test_convert_json_bad_json_raises_with_original_text() -> None:
    """`convert_json(b'{bad json')` 抛 `ValueError` 且消息含原文前 200 字符。"""
    raw = b"{bad json"
    with pytest.raises(ValueError) as exc:
        convert_json(raw)

    assert "{bad json" in str(exc.value)
    assert "200" in str(exc.value)


def test_convert_json_error_truncates_long_text_to_200_chars() -> None:
    """超长原文只回显前 200 字符：够定位问题行，又不会把日志刷满。"""
    raw = "{" + "x" * 500
    with pytest.raises(ValueError) as exc:
        convert_json(raw)

    message = str(exc.value)
    assert raw[:200] in message
    assert raw[:201] not in message


@pytest.mark.parametrize("value", [0, False, "0"])
def test_convert_json_preserves_falsy_json_values(value) -> None:
    """JSON 里的 0 / false / "0" 是有效载荷，不能被当成"空值"丢掉。"""
    assert convert_json(json.dumps(value)) == value


def test_convert_json_non_ascii_survives() -> None:
    """中文不因编码往返而损坏。"""
    assert convert_json('{"作物": "小麦"}') == {"作物": "小麦"}


# ---- convert_bool ----


@pytest.mark.parametrize(("value", "expected"), [(1, True), (0, False), (b"1", True), (b"0", False), (None, None)])
def test_convert_bool_accepts_only_zero_and_one(value, expected) -> None:
    """`convert_bool(1) is True`、`convert_bool(0) is False`、`convert_bool(None) is None`。"""
    result = convert_bool(value)
    assert result is expected


def test_convert_bool_rejects_two_with_column_name() -> None:
    """`convert_bool(2, column="device.status")` 抛 `ValueError` 且消息含列名。

    "报错而不是宽松处理"是关键：`device.status=2` 表示"故障"，把它当 True 会让
    "设备状态永远不是故障"这类问题到线上才被发现。
    """
    with pytest.raises(ValueError) as exc:
        convert_bool(2, column="device.status")

    message = str(exc.value)
    assert "device.status" in message
    assert "2" in message
    assert "smallint" in message


@pytest.mark.parametrize("value", [-1, 3, 99])
def test_convert_bool_rejects_any_non_boolean_value(value) -> None:
    """其它任何非 0/1 值同样报错，不做截断或取整。"""
    with pytest.raises(ValueError):
        convert_bool(value, column="crop.is_main")


def test_convert_bool_without_column_still_reports() -> None:
    """未传列名时消息仍可读（不出现 "None 出现非布尔值"）。"""
    with pytest.raises(ValueError) as exc:
        convert_bool(5)

    assert "该列" in str(exc.value)


# ---- convert_datetime ----


def test_convert_datetime_shanghai_local_to_utc() -> None:
    """`convert_datetime("2026-08-30 12:00:00")` 的返回值 `astimezone(UTC).hour == 4`。

    用 `replace(tzinfo=UTC)` 会得到 hour == 12，前端显示整体晚 8 小时。
    """
    result = convert_datetime("2026-08-30 12:00:00")
    assert result is not None
    assert result.astimezone(UTC).hour == 4
    assert result.utcoffset() == datetime.now(UTC).utcoffset()


def test_convert_datetime_accepts_naive_datetime_object() -> None:
    """datetime 对象入参（PyMySQL 的 DATETIME 返回值）与字符串等价。"""
    from_object = convert_datetime(datetime(2026, 8, 30, 12, 0, 0))
    from_string = convert_datetime("2026-08-30 12:00:00")
    assert from_object == from_string


def test_convert_datetime_none_returns_none() -> None:
    assert convert_datetime(None) is None


def test_convert_datetime_respects_source_tz_parameter() -> None:
    """`source_tz` 是参数而非硬编码：源库在 UTC 时不得再减 8 小时。"""
    result = convert_datetime("2026-08-30 12:00:00", source_tz="UTC")
    assert result is not None
    assert result.astimezone(UTC).hour == 12


def test_convert_datetime_keeps_aware_value_unchanged() -> None:
    """已带时区的值不再叠加源库时区，只做转换。"""
    aware = datetime(2026, 8, 30, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert convert_datetime(aware) == aware.astimezone(UTC)


def test_convert_datetime_default_tz_is_asia_shanghai() -> None:
    """默认时区是 Asia/Shanghai（中国部署现状），写进脚本的必填参数说明。"""
    assert DEFAULT_SOURCE_TZ == "Asia/Shanghai"


def test_convert_datetime_result_is_timezone_aware() -> None:
    """返回值必须带 tzinfo：naive 值写进 TIMESTAMPTZ 会依赖连接时区，不可控。"""
    result = convert_datetime("2026-08-30 12:00:00")
    assert result is not None
    assert result.tzinfo is not None


# ---- 导入期无副作用 ----


def test_import_has_no_side_effects() -> None:
    """模块可导入且不触发数据库连接、不读配置、不建文件。

    验收要求 `python -c "from scripts.migrate.type_converters import ..."` 可导入
    且无副作用——纯函数模块不得在导入期连库或读 `Settings`。
    """
    import scripts.migrate.type_converters as module

    assert not hasattr(module, "connect")
    assert not hasattr(module, "get_settings")
