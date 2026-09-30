"""规则层共享契约：`RuleResult` 与规则基类（T3-05，技术方案 11.1）。

`RuleResult` 是三类输入（环境事实、农事历、问诊回访）落到规则层后的统一条目形态，
字段与 T3-01 的 `prompt_item` 列一一对应，落库时不需要再做一轮字段翻译。三类输入各自
的采集模块（`app/broadcast/inputs/*`）直接构造本结构，规则类（`rules/*`）的 `evaluate`
也返回本结构。

`frozen=True` 让结果不可变：每条结果都要经过合并、去重、排序、截断四道处理，可变对象
在过程中被就地修改会让"这条为什么被抑制"难以复现。

本模块只依赖 `app.agent.state` 与标准库，不导入流程配置或数据库，避免与上层形成循环。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.agent.state import ProcessId

if TYPE_CHECKING:  # 仅供类型标注，运行时不导入，避免规则与引擎互相依赖
    from app.broadcast.rules.engine import RuleContext


@dataclass(frozen=True)
class RuleResult:
    """一条规则命中结果。五段式（呼告、级别、事实、影响、行动）由产品说明书 4.3.3 规定。"""

    source_kind: str  # env_risk / calendar / followup
    severity: str  # urgent / notice / reminder
    callout: str  # 呼告（对象）
    fact: str  # 事实
    impact: str  # 影响
    action: str  # 行动
    rule_id: str
    facts_provenance: dict[str, str] = field(default_factory=dict)
    timeliness_score: float = 0.0  # 供 T3-06 排序
    relevance_score: float = 0.0  # 供 T3-06 排序


class BaseRule(ABC):
    """规则基类。按流程筛选靠类属性声明，不用按流程标识逐条判断的分支。

    `process_ids` 为 `None` 表示全流程适用；`requires_env=True` 的规则在
    `EnvFacts.source == "none"` 时被引擎整体跳过（技术方案 11.2）。
    """

    process_ids: frozenset[ProcessId] | None = None
    requires_env: bool = False
    source_kind: str = "env_risk"

    def applies_to(self, process_id: ProcessId) -> bool:
        """本规则是否适用于该流程。"""
        return self.process_ids is None or process_id in self.process_ids

    @abstractmethod
    def evaluate(self, ctx: "RuleContext") -> list[RuleResult]:
        """在给定上下文下产出命中条目。实现必须是纯确定性逻辑，不得调用模型。"""
        raise NotImplementedError


__all__ = ["BaseRule", "RuleResult"]
