"""优先级排序与条数控制（T3-06，技术方案 11.3）。

对规则层产出的条目做合并去重、按"紧急程度 → 时效性 → 相关度"排序、并按条数上限截断。
全是纯函数：不涉异步、不碰数据库，便于单测，也便于产品复盘时复现"当时为什么展示的是这三条"。

三条规则：

1. **排序用元组而非加权求和**。加权求和的三个系数无法在无真实数据前定标，且一旦定标就
   解释了"为什么这条排在前面"这个问题——元组比较的顺序本身就是答案。三项都取负号是因为
   排序要降序（`sorted` 默认升序），Python 元组逐项比较，因此紧急程度权重天然高于时效性、
   时效性天然高于相关度。
2. **去重粒度是 `rule_id`，保留优先级最高的一条**。三类输入可能出现同一件事的重复条目。
   先 `sorted` 再 `setdefault` 是关键：直接遍历原列表 `setdefault` 保留的是"先出现的那条"，
   与优先级无关，结果会随采集顺序变化。
3. **普通提示 ≤ 3 条、紧急不受限，且被截断条目保留**。紧急代表"要出事了"的级别，被"今天
   已经三条了"挤掉是产品不能接受的失败；被截断条目返回并落库，产品复盘"哪些内容长期被
   压制"依赖它（`suppressed=True`），只在内存丢弃就无从统计。
"""

from collections.abc import Sequence

from app.broadcast.rules.base import RuleResult

# 级别权重。urgent > notice > reminder。
SEVERITY_WEIGHT: dict[str, int] = {"urgent": 3, "notice": 2, "reminder": 1}

# 普通提示每日展示上限（产品说明书附录 A3）。紧急提示不参与此上限。
NORMAL_LIMIT = 3

# `priority_score` 落库时的合成系数。与 `priority_key` 必须保持单调一致：
# `SEVERITY_WEIGHT` 权重 100、时效性 10、相关度 1，三项都落在 [0,1] 时分数不溢出
# `Numeric(8, 3)` 且大小关系与排序键一致。
_SCORE_SEVERITY = 100
_SCORE_TIMELINESS = 10
_SCORE_RELEVANCE = 1


def _severity_weight(severity: str) -> int:
    """级别权重；未知级别按 notice 处理，避免 `KeyError` 中断整批生成。"""
    return SEVERITY_WEIGHT.get(severity, SEVERITY_WEIGHT["notice"])


def priority_key(item: RuleResult) -> tuple[int, float, float]:
    """排序键：紧急程度 → 时效性 → 与当前关注点相关度。"""
    return (
        -_severity_weight(item.severity),  # urgent > notice > reminder
        -item.timeliness_score,  # 越紧迫越高
        -item.relevance_score,  # 与近期提问/回访的相关度
    )


def priority_score(item: RuleResult) -> float:
    """落库用的合成分数：三维拼接为稳定、可复现的数字。

    公式与 `priority_key` 单调一致——用同一组权重生成，避免两套排序逻辑漂移。分数只在
    生成快照这一次调用内有意义，落库后成为可供产品复盘的排序依据。
    """
    return round(
        _severity_weight(item.severity) * _SCORE_SEVERITY
        + item.timeliness_score * _SCORE_TIMELINESS
        + item.relevance_score * _SCORE_RELEVANCE,
        3,
    )


def merge_and_rank(items: list[RuleResult]) -> list[RuleResult]:
    """合并去重后按优先级降序排列，同 `rule_id` 保留优先级最高的一条。

    先 `sorted` 再 `setdefault`：遍历顺序因此与优先级挂钩，结果不随输入采集顺序变化。
    条目数为个位数，`O(n log n)` 无实际影响。
    """
    by_rule: dict[str, RuleResult] = {}
    for item in sorted(items, key=priority_key):
        by_rule.setdefault(item.rule_id, item)
    return sorted(by_rule.values(), key=priority_key)


def apply_limit(items: list[RuleResult]) -> tuple[list[RuleResult], list[tuple[RuleResult, bool]]]:
    """普通提示限 3 条，紧急不受限；被截断条目仍返回并携带 `suppressed=True` 标记。

    返回 `(kept, dropped)`。`kept` 与 `dropped` 都需落库：`dropped` 的条目在快照里以
    `suppressed=True` 保存，供产品复盘"哪些内容长期被压制"，但不出现在接口返回的展示列表中。

    `RuleResult` 是 `frozen=True` 的不可变对象且没有 `suppressed` 字段，抑制标记因此随条目
    以 `(条目, True)` 二元组一并返回，由消费方（T3-08）在建 `PromptItem` 时写入该列——不在
    此处就地改对象，与"结果不可变、被抑制可复现"的设计一致。
    """
    ordered = sorted(items, key=priority_key)
    urgent = [i for i in ordered if i.severity == "urgent"]
    normal = [i for i in ordered if i.severity != "urgent"]
    kept = urgent + normal[:NORMAL_LIMIT]
    dropped = [(i, True) for i in normal[NORMAL_LIMIT:]]
    return kept, dropped


def rank_and_limit(items: Sequence[RuleResult]) -> tuple[list[RuleResult], list[tuple[RuleResult, bool]]]:
    """合并去重 → 排序 → 截断的组合入口，供 T3-08 一次调用。"""
    return apply_limit(merge_and_rank(list(items)))


__all__ = [
    "NORMAL_LIMIT",
    "SEVERITY_WEIGHT",
    "apply_limit",
    "merge_and_rank",
    "priority_key",
    "priority_score",
    "rank_and_limit",
]
