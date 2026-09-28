"""闸门门面：引擎 + 审计 + 影子模式（T2-46，技术方案 14.2 代码原文落地）。

`GateService.review` 是 T2-45 子图节点的唯一入口，把三件事串起来：

1. `GateEngine.evaluate` 执行全部规则并裁定（fail-closed）；
2. `GateAuditWriter.record` 落审计（fail-open，见 `app/gate/audit.py`）；
3. 影子模式：`GATE_MODE=shadow` 时返回 `GateResult(passed=True, shadowed=result)` 的
   "放行外壳"，真实判定留在内层。

**审计在影子分支之前调用**：影子模式只影响"是否拦截"，不影响审计口径——记录的始终是真实
判定并以 `shadowed=True` 标记。若不标记，事后无法区分"真实拦截"与"影子期记录"，14.2 要求
的"人工抽查被拦但不该拦的比例"将无法筛选，14.3 的"闸门拦截率"也会被影子行污染。

影子模式的作用是"让规则先跑起来积累数据"，**不是**"让农户看到被拦截后的处方"：T2-45 的
节点按 `result.shadowed.decision` 决定渲染哪一版文本，影子期农户看到的仍是真实判定后的结果。
"""

from app.core.config import Settings, get_settings
from app.gate.audit import GateAuditWriter
from app.gate.contracts import GateContext, GateResult
from app.gate.engine import GateEngine
from app.repositories.gate_audit import GateAuditRepository


class GateService:
    """闸门门面：引擎 + 审计 + 影子模式。决策语义见 `GateResult` docstring。"""

    def __init__(
        self,
        engine: GateEngine | None = None,
        audit: GateAuditWriter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.engine = engine or GateEngine()
        self.audit = audit or GateAuditWriter(GateAuditRepository())
        self.settings = settings or get_settings()

    async def review(self, draft: object, ctx: GateContext) -> GateResult:
        """执行判定、落审计，并按 `GATE_MODE` 决定是否套影子外壳。

        `draft` 声明为 `object` 而非 agent 侧的 `Prescription`：闸门不得导入生成链路（L1），
        它只被 `GateAuditWriter` 按鸭子类型转成镜像用于审计快照，不参与规则计算——规则读的是
        `ctx.prescription`，即从 state 取出的同一份处方镜像。
        """
        result = self.engine.evaluate(ctx)
        await self.audit.record(draft, ctx, result, shadowed=self.settings.GATE_MODE == "shadow")
        if self.settings.GATE_MODE == "shadow":
            return GateResult(passed=True, shadowed=result)  # 只记不拦（仅灰度期）
        return result


__all__ = ["GateService"]
