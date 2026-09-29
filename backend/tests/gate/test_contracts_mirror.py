"""T2-34：闸门镜像枚举与生成链路取值集合逐一相等。

闸门不得导入 `app.agent`（L1），因此 `ProcessId` / `ViolationSeverity` / `GateDecision` 在
`app/gate/enums.py` 是**同值不同源**的镜像。镜像的代价是可能漂移——生成链路加了流程而闸门
没跟上，或裁定档位改了两侧不一致——本文件就是防止漂移的那道闸：逐一断言两侧取值集合相等。
`GateRuleId` 另与配置层的 `app.process_config.rule_ids.GateRuleId` 同值，一并核对。
"""

from app.agent.state import GateDecision as AgentGateDecision
from app.agent.state import ProcessId as AgentProcessId
from app.agent.state import ViolationSeverity as AgentViolationSeverity
from app.gate.enums import GateDecision, GateRuleId, ProcessId, ViolationSeverity
from app.process_config.rule_ids import GateRuleId as ConfigGateRuleId


def _values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


def test_process_id_mirror_matches_agent_state() -> None:
    """七值流程标识两侧一致：闸门按同一个 `process_id` 选红线，错一个就对不上配置。"""
    assert _values(ProcessId) == _values(AgentProcessId)
    assert len(ProcessId) == 7


def test_violation_severity_mirror_matches_agent_state() -> None:
    """配置层严重度两值一致（block / warn）。"""
    assert _values(ViolationSeverity) == _values(AgentViolationSeverity)
    assert len(ViolationSeverity) == 2


def test_gate_decision_mirror_matches_agent_state() -> None:
    """裁定三值一致（allow / degrade / block），与规则等级三值一一对应。"""
    assert _values(GateDecision) == _values(AgentGateDecision)
    assert len(GateDecision) == 3


def test_gate_rule_id_mirror_matches_config_layer() -> None:
    """闸门注册表用的规则 id 与配置加载期常量同值（十二条）。"""
    assert _values(GateRuleId) == _values(ConfigGateRuleId)
    assert len(GateRuleId) == 12


def test_mirror_enums_are_distinct_types() -> None:
    """镜像必须是不同源的类型：同值但不同类，证明没有反向依赖。"""
    assert ProcessId is not AgentProcessId
    assert ViolationSeverity is not AgentViolationSeverity
    assert GateDecision is not AgentGateDecision
