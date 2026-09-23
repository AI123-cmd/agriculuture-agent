"""物候期农事历：按「作物 + 生育期」给出阶段农事提醒。

种子数据当前内置在代码中，运营需要扩充时可迁移到独立表承载。"""

from .rules.base import RuleResult

# 农事历：作物 -> 生育期关键词 -> (标题, 内容)
_CALENDAR: dict[str, dict[str, tuple[str, str]]] = {
    "水稻": {
        "返青": ("追施分蘖肥", "返青后及时追施分蘖肥（约占总氮肥30%），促进早发快发。"),
        "分蘖": ("控苗晒田", "分蘖后期适时晒田控苗，改善根系活力，控制无效分蘖。"),
        "抽穗": ("施穗肥防稻瘟", "倒二叶露尖时施穗肥，重点防治稻瘟病。"),
    },
    "小麦": {
        "返青": ("追施返青肥", "返青期根据苗情追施返青肥，促弱转壮。"),
        "抽穗": ("防治赤霉病", "抽穗扬花期关注赤霉病，落实一喷三防。"),
    },
    "玉米": {
        "大喇叭口": ("重施穗肥", "大喇叭口期重施穗肥，注意防治大斑病。"),
    },
}


def phase_tasks(crop_name: str, growth_stage: str, month: int | None = None) -> list[RuleResult]:
    """根据作物与生育期返回阶段农事提醒；未命中返回空列表。"""
    if not crop_name:
        return []

    crop_cal = _CALENDAR.get(crop_name)
    if not crop_cal:
        return []

    results: list[RuleResult] = []
    for keyword, (title, content) in crop_cal.items():
        if growth_stage and keyword in growth_stage:
            results.append(
                RuleResult(
                    type="calendar",
                    level="normal",
                    title=title,
                    content=content,
                    source="calendar",
                    action=title,
                )
            )
    return results
