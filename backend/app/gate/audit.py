"""闸门审计写入（T2-46，技术方案 8.5）。

`GateAuditWriter.record` 是"审计不能成为可用性瓶颈"的落点：写入失败被吞掉（fail-open），
但必须留结构化错误日志与 `gate.audit.write_failed` 指标，供 T6-09 告警。这条 fail-open 与
引擎的 fail-closed 刻意相反——引擎保护"不放过不合格处方"，审计保护"判定本身可完成"：
若审计写库失败就拒绝出方，数据库抖动会直接变成服务不可用，而判定结果本身是正确的。

"吞掉"必须伴随后果，否则审计缺失会变成静默：错误日志的 event 固定为
`gate_audit_write_failed`，指标名固定为 `gate.audit.write_failed`，两者都被监控与告警覆盖。

`prescription_snapshot` 存处方镜像的全量 `model_dump(mode="json")`，是表中最重的字段；
`violations` 存 `GateViolation` 的 JSON 数组而非拼接字符串——8.5 三条复盘用途都要按
`rule_id`、`severity`、`field` 聚合，结构化才能查。
"""

from uuid import uuid4

from app.gate.contracts import GateContext, GatePrescription, GateResult
from app.models.gate_audit import GateAudit
from app.observability import log, metrics
from app.repositories.gate_audit import GateAuditRepository

# 指标名与日志 event 名集中在此：告警规则与单测都按这两个字符串定位，散落会漂移。
WRITE_FAILED_EVENT = "gate_audit_write_failed"
WRITE_FAILED_METRIC = "gate.audit.write_failed"


class GateAuditWriter:
    """闸门审计写入。写入失败不得影响判定结果（审计不能成为可用性瓶颈）。"""

    def __init__(self, repo: GateAuditRepository) -> None:
        self._repo = repo

    async def record(
        self,
        draft: GatePrescription | object,
        ctx: GateContext,
        result: GateResult,
        *,
        shadowed: bool = False,
    ) -> None:
        """落一行审计。失败即吞，但记日志与指标。

        `draft` 声明为宽类型而非 agent 侧的 `Prescription`：闸门不得导入生成链路（L1），
        故只做鸭子类型转换（`GatePrescription.from_contract` 同时接受对象与 dict）。
        """
        snapshot = GatePrescription.from_contract(draft)
        row = GateAudit(
            id=str(uuid4()),
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            process_id=ctx.process_id.value,
            config_version=ctx.config_version,
            decision=result.decision,
            shadowed=shadowed,
            violations=[v.model_dump(mode="json") for v in result.violations],
            checked_rules=list(result.checked_rules),
            prescription_snapshot=snapshot.model_dump(mode="json"),
        )
        try:
            await self._repo.add(row)
        except Exception:  # noqa: BLE001 —— 审计写入吞掉一切异常，见模块 docstring
            log.error(
                WRITE_FAILED_EVENT,
                process_id=ctx.process_id.value,
                decision=result.decision,
                exc_info=True,
            )
            metrics.incr(WRITE_FAILED_METRIC)


__all__ = ["WRITE_FAILED_EVENT", "WRITE_FAILED_METRIC", "GateAuditWriter"]
