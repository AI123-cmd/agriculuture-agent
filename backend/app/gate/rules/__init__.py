"""闸门规则实现包（T2-34 起）。

导入本包会触发全部规则模块的导入，从而执行各自的 `@register`。`build_rules()` 依赖这一
副作用：只要包被导入，十二条规则 id 就都已登记。新增规则文件时必须在此追加导入，否则该
规则不会被引擎加载（`build_rules` 会在 `missing` 分支报错，不会静默跳过）。

十二条 id 分布在八个实现文件里：`R-DOSAGE`/`R-MAX-USE` 同文件，`R-PROVENANCE`/`R-NO-DATA`
分别见 `provenance.py` 与 `presentation.py`。
"""

from app.gate.rules import (
    dosage,
    medication_conflict,
    pesticide_whitelist,
    presentation,
    process_redline,
    provenance,
    redline_load,
    region_fitness,
    registered_crop,
    safety_interval,
)
from app.gate.rules.base import GateConfigError, Rule, build_rules, register, registered_rule_ids

__all__ = [
    "GateConfigError",
    "Rule",
    "build_rules",
    "dosage",
    "medication_conflict",
    "pesticide_whitelist",
    "presentation",
    "process_redline",
    "provenance",
    "redline_load",
    "region_fitness",
    "register",
    "registered_crop",
    "registered_rule_ids",
    "safety_interval",
]
