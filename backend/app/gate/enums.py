"""闸门侧枚举镜像（与 `app.agent.state` 同值不同源）。

L1 硬规则禁止 `app.gate` 导入 `app.agent`（`scripts/check_layers.py`），但闸门要判定的
流程、裁定档位与规则 id 又必须与生成链路保持同值。解法是**镜像**：在闸门包内定义一份
取值完全相同的枚举，靠 `tests/gate/test_contracts_mirror.py` 断言两侧取值集合逐一相等来
同步。宁可多一份枚举与一条测试，也不让闸门对生成链路产生编译期依赖——闸门要能脱离智能体
层单独部署与单测，这是"生成与校验物理隔离"的落地方式。

`GateRuleId` 与 `app/process_config/rule_ids.py` 同值：前者供闸门注册表白名单使用，后者
是配置加载期的规则 id 常量。两侧靠同一份"技术方案 8.2 规则清单"对齐。
"""

from enum import Enum
from typing import Literal

# 规则判定的严重度档位。技术方案 8.2 的"违规等级"列给出三个取值，与 `GateDecision`
# 的三值一一对应（block→block、degrade→degrade、warn→allow 但留痕）。故意比
# `ViolationSeverity`（两值，给配置层用）多一个 `degrade`：取消 `degrade` 会让
# "不通过但不属严重违规"这一档无处安放，`tea.yaml` 的 `TEA-05` 就明确写 `severity: degrade`。
GateSeverity = Literal["block", "degrade", "warn"]


class ProcessId(str, Enum):
    """流程标识。七值固定，与 `app.agent.state.ProcessId` 同值。"""

    RICE = "rice"  # 水稻
    TEA = "tea"  # 茶
    RAPESEED = "rapeseed"  # 油菜
    CHILI = "chili"  # 辣椒
    WEED = "weed"  # 杂草防治
    PIG = "pig"  # 养猪
    GENERAL = "general"  # 通用农业兜底


class ViolationSeverity(str, Enum):
    """配置层违规严重度。两值，与 `app.agent.state.ViolationSeverity` 同值。"""

    BLOCK = "block"
    WARN = "warn"


class GateDecision(str, Enum):
    """闸门裁定。三值一一对应规则等级，与 `app.agent.state.GateDecision` 同值。"""

    ALLOW = "allow"
    DEGRADE = "degrade"
    BLOCK = "block"


class GateRuleId(str, Enum):
    """闸门规则 id。十二条取值来自技术方案 8.2 规则清单。"""

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


__all__ = ["GateDecision", "GateRuleId", "GateSeverity", "ProcessId", "ViolationSeverity"]
