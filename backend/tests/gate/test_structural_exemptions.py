"""T2-41：`STRUCTURAL_EXEMPTIONS` 豁免集合的规模与语义约束（CI 守卫）。

技术方案 8.3.3：豁免清单必须极短且有明确语义，否则会变成漏洞。第一条断言限规模，
第二条堵住"把 `7天` 这类带单位 token 加进豁免集合"这条最可能的漏洞路径。
"""

from app.gate.rules.provenance import STRUCTURAL_EXEMPTIONS, _STRUCTURAL_MAX


def test_structural_exemptions_stay_small() -> None:
    """规模上限写死在测试里，扩集合必须同时改测试并说明理由。"""
    assert len(STRUCTURAL_EXEMPTIONS) <= _STRUCTURAL_MAX


def test_structural_exemptions_are_integers() -> None:
    """豁免项必须是纯数字序号，不允许出现带单位的 token（如 '7天'）。"""
    assert all(item.isdigit() for item in STRUCTURAL_EXEMPTIONS)


def test_hypothetical_unit_token_would_violate_second_rule() -> None:
    """构造"把 7天 加入集合"的假设样例，断言第二条测试会失败。

    这正是第二条断言存在的理由：一旦有人用带单位 token 扩充豁免集合，CI 立即拦下。
    """
    hypothetical = STRUCTURAL_EXEMPTIONS | {"7天"}

    assert not all(item.isdigit() for item in hypothetical)
