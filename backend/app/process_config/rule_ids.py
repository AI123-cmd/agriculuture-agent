"""闸门规则 id 常量与期望载荷规格表。

规则 id 与技术方案 8.2 的规则清单逐条对应。用枚举而不是散字符串，让「配置里写了一个
不存在的规则 id」在加载阶段就能被抓到——否则这条红线会一直存在但永不生效，属于最难
排查的一类配置错误。

`RULE_PAYLOAD_SPEC` 只声明键名的**合法集合**，不校验收值：取值的正确性属于农技内容，
由人工确认，工具能判断的只是「这个键不属于本规则」。
"""

from enum import Enum


class GateRuleId(str, Enum):
    """闸门规则 id。全部取值来自技术方案 8.2 规则清单。"""

    WHITELIST = "R-WHITELIST"
    REGISTERED_CROP = "R-REGISTERED-CROP"
    DOSAGE = "R-DOSAGE"
    INTERVAL = "R-INTERVAL"
    MAX_USE = "R-MAX-USE"
    CONFLICT = "R-CONFLICT"
    REGION = "R-REGION"
    PROVENANCE = "R-PROVENANCE"
    REDLINE = "R-REDLINE"
    REDLINE_LOAD = "R-REDLINE-LOAD"
    CAUTION = "R-CAUTION"
    NO_DATA = "R-NO-DATA"


# 每条规则对该规则专属 data 载荷的期望键名。`None` 表示该规则不接受 data：
# 这类规则（如 R-WHITELIST）的判据完全来自知识库，配置侧补充的载荷无从被执行，
# 因此填了 data 必须报错，否则会给人「已经配置了额外约束」的错觉。
RULE_PAYLOAD_SPEC: dict[GateRuleId, set[str] | None] = {
    GateRuleId.REDLINE: {"forbidden", "forbidden_phrases", "reference", "labels", "min_days", "notice"},
    GateRuleId.REGION: {"regions", "standards"},
    GateRuleId.INTERVAL: {"min_days", "by_pesticide"},
    GateRuleId.MAX_USE: {"limit"},
    GateRuleId.DOSAGE: {"unit", "min", "max"},
    GateRuleId.REGISTERED_CROP: None,
    GateRuleId.WHITELIST: None,
    GateRuleId.CONFLICT: None,
    GateRuleId.PROVENANCE: None,
    GateRuleId.REDLINE_LOAD: None,
    GateRuleId.CAUTION: None,
    GateRuleId.NO_DATA: None,
}


def parse_rule_id(token: str) -> GateRuleId | None:
    """解析单个规则 id，未登记返回 `None`。

    返回 `None` 而不是抛异常：调用方（红线校验器）要把「引用了不存在的规则」作为
    一条问题汇总进结果对象，而不是中断整个校验。
    """
    try:
        return GateRuleId(token)
    except ValueError:
        return None


__all__ = ["RULE_PAYLOAD_SPEC", "GateRuleId", "parse_rule_id"]
