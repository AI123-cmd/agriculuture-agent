"""T2-39：`app/gate/matching.py` 的窗口过滤与文本/身份归一。

`R-MAX-USE` 与 `R-CONFLICT` 读同一份用药史，若各自实现一遍窗口过滤，两处会漂移，
出现"次数按本季算、冲突按全年算"这类难以察觉的不一致，而这类不一致在安全规则里直接
等于漏判。因此窗口过滤与归一抽到共用模块，本文件锁定这三条口径。
"""

from datetime import date

from app.gate.contracts import MedicationRecord
from app.gate.matching import conflicts_with, identity_key, normalize_text, recent_records

SEASON_START = date(2025, 5, 1)


def _record(name: str, used_on: date, knowledge_id: str | None = "K-1001") -> MedicationRecord:
    return MedicationRecord(knowledge_id=knowledge_id, pesticide_name=name, used_on=used_on)


def test_recent_records_returns_all_when_season_start_none() -> None:
    """`season_start=None` 时不过滤，返回全量（"每季"边界由调用方按流程给定）。"""
    history = [
        _record("吡虫啉", date(2024, 1, 1)),
        _record("代森锰锌", date(2025, 6, 1)),
    ]

    assert recent_records(history, None) == history


def test_recent_records_filters_by_inclusive_lower_bound() -> None:
    """窗口下界为 `>=`：恰在 `season_start` 当天的记录纳入，前一天排除。"""
    on_boundary = _record("吡虫啉", SEASON_START)
    before = _record("代森锰锌", date(2025, 4, 30))
    history = [before, on_boundary]

    assert recent_records(history, SEASON_START) == [on_boundary]


def test_identity_key_strips_whitespace_and_form_suffix() -> None:
    """全半角统一、去空白、去剂型后缀括号后两者相等。"""
    assert identity_key(" 吡虫啉（可湿性粉剂） ") == identity_key("吡虫啉")


def test_identity_key_strips_ascii_parentheses_too() -> None:
    """半角括号同样被去除。"""
    assert identity_key("吡虫啉(wettable powder)") == "吡虫啉"


def test_normalize_text_unifies_fullwidth_and_removes_whitespace() -> None:
    """文本归一：全角转半角、去空白，用于子串匹配前两侧统一处理。"""
    assert normalize_text("乐 果") == normalize_text("乐果")
    assert normalize_text("３０克") == normalize_text("30克")


def test_conflicts_with_returns_entry_combos_hit_by_recent() -> None:
    """只查"条目禁忌清单是否被近期用药命中"这一个方向。"""
    recent = [_record("代森锰锌（可湿性粉剂）", date(2025, 5, 10))]

    hits = conflicts_with(("代森锰锌", "波尔多液"), recent)

    assert hits == ["代森锰锌"]


def test_conflicts_with_empty_when_no_overlap() -> None:
    recent = [_record("吡虫啉", date(2025, 5, 10))]

    assert conflicts_with(("代森锰锌",), recent) == []
