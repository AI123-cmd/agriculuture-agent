"""规则注册表与按流程筛选（T3-05，技术方案 11.1）。

`load_rules` 返回一次匹配要跑的全部规则。**不引入 `if process_id ==` 分支**：规则若只为
某条流程存在（如水稻的抗药性暂停提示），在类上声明 `process_ids={ProcessId.RICE}` 即可，
引擎按 `applies_to` 过滤。全局约定第六节要求"新增一条流程的改动量应控制为一份 YAML + 一个
约 15 行的子图文件 + 路由表注册一行"，规则层若遍地是流程判断，新增流程就要改每一处。

当前注册的是环境阈值的七条（六条单指标 + 一条温湿组合）。这三类输入里的农事历与问诊回访
两路本身就是确定性产出，不经本注册表：它们由 `inputs/*` 直接构造 `RuleResult`，引擎在
`match_all` 里与规则命中合并（技术方案 11.2）。
"""

from collections.abc import Sequence

from app.broadcast.rules.base import BaseRule
from app.broadcast.rules.pest_risk import PestRiskRule
from app.broadcast.rules.threshold import default_threshold_rules


def load_rules() -> list[BaseRule]:
    """装配规则层的全部规则实例。

    每次调用都返回新实例：阈值规则在构造时读取 `get_settings()`，测试会 monkeypatch
    配置，共享实例会让"改了配置但规则还是旧阈值"。规则本身无状态，重建成本可忽略。
    """
    return [*default_threshold_rules(), PestRiskRule()]


def rule_ids(rules: Sequence[BaseRule]) -> set[str]:
    """规则实例产出的 `rule_id` 集合。

    阈值规则与温湿组合规则的 `rule_id` 是类/实例属性；本函数把它们摊平成集合，供 T3-06/
    T3-07 交叉校验"模板与紧急规格是否覆盖了每条规则"。
    """
    ids: set[str] = set()
    for rule in rules:
        rid = getattr(rule, "rule_id", None)
        if isinstance(rid, str):
            ids.add(rid)
    return ids


__all__ = ["load_rules", "rule_ids"]
