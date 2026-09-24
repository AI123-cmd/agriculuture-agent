"""流程配置层：可选源（seeds）与运行时配置的单一事实来源。

本包是纯数据层，不依赖数据库、不依赖智能体层（技术方案 15.4）。流程之间的
全部差异都必须落在配置字段里，编排代码中不得出现 `if process_id == ...`。
"""

from .errors import ConfigIntegrityError, ConfigLoadError, ConfigUnavailableError
from .models import (
    ConfusablePair,
    FollowupPolicy,
    FollowupTaskSpec,
    OfflineQuestion,
    OutputTemplateSpec,
    ProcessConfig,
    RedlineSpec,
    SlotSpec,
    TerminologySpec,
    TimingRule,
)
from .redlines import LoadedRedlines, Redline, RedlineValidator, build_availability
from .rule_ids import RULE_PAYLOAD_SPEC, GateRuleId

__all__ = [
    "RULE_PAYLOAD_SPEC",
    "ConfusablePair",
    "ConfigIntegrityError",
    "ConfigLoadError",
    "ConfigUnavailableError",
    "FollowupPolicy",
    "FollowupTaskSpec",
    "GateRuleId",
    "LoadedRedlines",
    "OfflineQuestion",
    "OutputTemplateSpec",
    "ProcessConfig",
    "Redline",
    "RedlineSpec",
    "RedlineValidator",
    "SlotSpec",
    "TerminologySpec",
    "TimingRule",
    "build_availability",
]
