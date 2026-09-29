"""按任务类型的降级出口与零模型模式（T2-58，技术方案 10.3）。

降级链的最后一跳：主供应商 A、同档备选 B、私有化模型全部失败后，网关抛
`AllProvidersFailedError`，由智能体基类按 `TaskType` 分派到本模块定义的出口。网关是
通用组件，不知道各智能体的降级语义（问诊降级为模板问题、判定降级为不确定），因此这张
映射表放在网关之外，只提供"任务类型 → 出口"的数据与零模型模式开关。

**四个出口全部是模板或规则产出，不需要模型**——这正是"降级仍服务"能成立的原因：模型
只在"把话说顺"这一步有用，事实与结构都由代码提供。

零模型模式（`ZeroModelMode`）是"规则层能力不依赖大模型"的落地：快照生成器在此模式下
全部提示条目直接用模板原文输出，仅损失文案的口语化程度，不损失事实准确性。它的硬前提
是**模板必须本身可读**，`validate_template_readable` 把它变成可断言的事实。
"""

import re
from enum import Enum

from app.llm.enums import TaskType


class DegradeExit(str, Enum):
    """任务级降级出口。"""

    TEMPLATE_QUESTION = "template_question"  # 问诊：模板问题
    UNCERTAIN_WITH_GUIDE = "uncertain_with_guide"  # 判定：不确定加求助引导
    REQUIRE_TECH_STATION = "require_tech_station"  # 处方：requires_tech_station=true
    TEMPLATE_TEXT = "template_text"  # 快照文案：模板原文


class UnreadableTemplate(Exception):
    """模板在零模型模式下不可读：含未替换占位符。"""


# 方案 10.3 的四条出口与各 TaskType 对齐。每一条都有对应，装配期无需再做"缺项回退"
# ——缺项本身即配置错误，由测试断言覆盖全量。
TASK_DEGRADE_EXIT: dict[TaskType, DegradeExit] = {
    TaskType.TRIAGE: DegradeExit.UNCERTAIN_WITH_GUIDE,  # 回退 general 并说明
    TaskType.INTERVIEW: DegradeExit.TEMPLATE_QUESTION,
    TaskType.DIAGNOSE: DegradeExit.UNCERTAIN_WITH_GUIDE,
    TaskType.PRESCRIBE: DegradeExit.REQUIRE_TECH_STATION,
    TaskType.SNAPSHOT_WRITER: DegradeExit.TEMPLATE_TEXT,
    TaskType.PANEL: DegradeExit.UNCERTAIN_WITH_GUIDE,  # 转单链判定
    TaskType.RETRIEVE: DegradeExit.UNCERTAIN_WITH_GUIDE,  # 返回空集
    TaskType.REPORT_GUARD: DegradeExit.UNCERTAIN_WITH_GUIDE,  # 有疑即报
}


# 未替换占位符的形态：`{{ ... }}`。单花括号不在拦截范围，因为正常文案里可能合法出现
# （如 JSON 片段），而双花括号在本项目的模板约定里专指待替换字段。
_PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}")


def validate_template_readable(template_text: str) -> None:
    """零模型模式的前置校验：模板渲染后不得含未替换占位符。

    若模板写成 `"{{metric}}超过{{threshold}}，建议{{action}}"`，零模型模式下渲染出来就是
    一句含占位符的残句。校验在模板加载时执行，发现不可读即报错——让"模板出事实"这条
    原则在配置阶段而不是产出阶段暴露。
    """
    if _PLACEHOLDER.search(template_text):
        raise UnreadableTemplate(f"模板存在未替换占位符: {template_text[:40]}")


class ZeroModelMode:
    """零模型模式：所有模型调用走模板原文，用于外部服务中断或显式关闭模型。

    触发条件有两个：全局开关（维护期主动关闭模型）与自动降级（网关候选链全部失败且
    任务是快照文案类）。两种触发都走到同一个"用模板原文"的实现。
    """

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    async def render_snapshot_item(self, template_text: str) -> str:
        """快照条目的零模型渲染：直接返回模板原文，不发起任何模型调用。"""
        return template_text


__all__ = [
    "TASK_DEGRADE_EXIT",
    "DegradeExit",
    "UnreadableTemplate",
    "ZeroModelMode",
    "validate_template_readable",
]
