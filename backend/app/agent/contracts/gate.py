"""门禁契约的**再导出**（T2-34 起契约本体归闸门所有）。

安全闸门是本方案**唯一拥有否决权**的组件，它的失败行为是 fail-closed：任何一处判断不出
结论，安全结论都是"不放行"，而不是按其余智能体的模式降级继续服务。这条差别被写成可构造性
约束（`process_redlines_loaded=False` 时构造不出 `decision="allow"`）。

T2-12 曾把 `GateResult`/`GateViolation`/`GateContext` 定义在本模块。T2-34 建立 `app/gate/`
时把它们迁到 `app/gate/contracts.py`，理由是 L1 分层规则禁止 `app.gate` 导入 `app.agent`：
若契约留在 agent 侧，闸门包就不得不反向依赖生成链路。**契约本体归闸门所有**，agent 侧改为
`from app.gate.contracts import ...` 再导出——L1 只禁止 gate → agent，此方向合法。

本模块因此只做转发，不含任何定义。原有导入点（`app.agent.contracts`）保持不变。
"""

from app.gate.contracts import (
    GateContext,
    GateResult,
    GateViolation,
    KnowledgeFacts,
)

__all__ = ["GateContext", "GateResult", "GateViolation", "KnowledgeFacts"]
