"""智能体层异常（T2-13）。

两个异常对应两种"失败"，必须分开：`ContractViolation` 是**可重试**的（模型这次没按
Schema 出，重出一次大概率能对），`AgentDegraded` 是**不可重试**的（连降级出口都构造
不出来，说明前提条件就不成立，再试只是浪费）。

`TransientModelError` 不在这里定义，它属于 `app/llm/errors.py`：网关侧的瞬时故障
（超时、限流、候选链全失败）由网关定义，智能体基类只是把它与 `ContractViolation`
一起列入可重试集合。异常定义跟着它的产生方走，而不是跟着消费方走。
"""


class ContractViolation(Exception):
    """契约违约：模型输出不满足输出契约。

    这是本项目的核心守卫之一（技术方案 5.2「解析失败即契约违约」）：`parsed` 为
    `None`、必填字段缺失、枚举越界、跨字段矛盾都走这一类。处理方式是"让模型重出"，
    绝不从文本里正则硬抽——农业场景下错误的字段值比没有字段值更危险。
    """


class AgentDegraded(Exception):
    """智能体无法完成降级：连最保守的产出都构造不出来。

    只在降级路径内部抛出（如问诊降级时该流程没有任何模板问题可用）。它与
    `ContractViolation` 的区别是"重试是否有意义"：这类前提缺失重试不会改变结果。
    """


__all__ = ["AgentDegraded", "ContractViolation"]
