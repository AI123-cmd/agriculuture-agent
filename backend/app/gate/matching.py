"""闸门内多条规则共用的匹配逻辑（T2-39、T2-42）。

窗口过滤与文本归一抽到共用模块，是因为多条规则读同一份用药史/展示文本：若各自实现一遍，
两处会漂移，出现"次数按本季算、冲突按全年算"这种难以察觉的不一致，而这类不一致在安全规则
里直接等于漏判。
"""

import re
import unicodedata
from datetime import date

from .contracts import MedicationRecord

# 剂型后缀：全角或半角括号内的一段内容。归一化时整体剥除。
_FORM_SUFFIX = re.compile(r"[（(].*?[)）]")


def recent_records(history: list[MedicationRecord], season_start: date | None) -> list[MedicationRecord]:
    """按季节窗口过滤用药史。`season_start` 为 None 时不过滤（全部纳入）。

    季节边界由调用方按流程给定，闸门不自行推断——"每季"可能是自然季也可能是作物季。
    """
    if season_start is None:
        return list(history)
    return [r for r in history if r.used_on >= season_start]


def normalize_text(text: str) -> str:
    """文本归一：全角转半角、去空白。用于文本匹配前的两侧统一处理。

    不做同义词扩展：禁用清单的措辞由农技确认，同义词应直接加进配置，而不是在代码里猜。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def identity_key(name: str) -> str:
    """药剂身份归一：去空白、去剂型后缀括号、统一全半角。用于重复用药判定。

    知识条目的 `forbidden_combinations` 与档案的 `pesticide_name` 来自不同录入方，
    同一药剂可能写作"吡虫啉"与"吡虫啉（可湿性粉剂）"，不归一会漏判禁忌。
    """
    normalized = unicodedata.normalize("NFKC", name).strip()
    normalized = _FORM_SUFFIX.sub("", normalized)
    return re.sub(r"\s+", "", normalized)


def conflicts_with(entry_combos: tuple[str, ...], recent: list[MedicationRecord]) -> list[str]:
    """返回与近期用药构成禁忌搭配的药剂名清单。

    只查"条目的禁忌清单是否被近期用药命中"这一个方向：反向数据往往缺失（历史记录可能
    没有对应知识条目），单向查询是"宁可少报也不误报"的取舍。
    """
    used = {identity_key(r.pesticide_name) for r in recent if r.pesticide_name}
    return [c for c in entry_combos if identity_key(c) in used]


__all__ = ["conflicts_with", "identity_key", "normalize_text", "recent_records"]
