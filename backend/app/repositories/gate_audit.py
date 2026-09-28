"""闸门审计仓储（T2-46，技术方案 8.5）。

本仓储与其他归属仓储形态不同：`gate_audit` 的写入者是闸门本身，它没有请求级
`AsyncSession`（闸门在子图节点里被调用，拿不到 FastAPI 的 `db` 依赖），因此本仓储
**构造时绑定会话工厂**，方法与 `ProcessConfigRepository` 一样自行开事务、不接受 `db`
参数。`GateService` 直接以无参形态构造它（`GateAuditRepository()`）。

为什么不复用 `OwnedRepository`：`OwnedRepository.add(db, user_id, obj)` 要求调用方传入
会话与归属用户，而审计写入的 fail-open 语义是"写失败不能拖垮判定"——由仓储自己管事务，
才能把"开事务、提交、异常回滚"整体包在 `GateAuditWriter` 的 try 里。

`gate_review_ticket` 不带 `user_id`（运营侧内部待办），其入队走本模块的
`enqueue_review_ticket`，与审计同源，避免调用方各自拼 SQL（L4）。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SessionLocal
from app.models.gate_audit import GateAudit
from app.models.gate_review_ticket import STATUS_PENDING, GateReviewTicket
from app.models.process_config import ProcessRedlineRow

_SessionFactory = async_sessionmaker[AsyncSession]


class GateAuditRepository:
    """闸门审计读写。构造时绑定会话工厂，方法自行开事务。"""

    def __init__(self, session_factory: _SessionFactory | None = None) -> None:
        self._session_factory = session_factory or SessionLocal

    async def add(self, row: GateAudit) -> GateAudit:
        """写入一行审计并提交。

        签名与 `GateAuditWriter.record` 的调用点一致（只传 row）：归属字段由
        `GateContext` 在构造 `GateAudit` 时填入，不在此重复校验——审计表本身不带
        "越权访问"风险（只写不按用户读），故不走 `OwnedRepository` 的归属注入。
        """
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
        return row

    async def blocks_for_review(self, limit: int = 100) -> list[GateAudit]:
        """用途一：block 事件人工复核。

        刻意不排除 `shadowed=True` 的行：灰度期的拦截正是要抽查的对象（14.2 要求
        人工评估"被拦但不该拦"的比例）。
        """
        stmt = select(GateAudit).where(GateAudit.decision == "block").order_by(GateAudit.created_at.desc()).limit(limit)
        async with self._session_factory() as db:
            return list((await db.execute(stmt)).scalars().all())

    async def degrade_rate(self, process_id: str, since: datetime) -> float:
        """用途二：某流程 degrade 率。

        口径对齐 14.3 的"闸门拦截率"：**排除 `shadowed=True` 的行**，因为影子期只记录
        不拦截，把影子行计入会低估强制期的真实比例。分母为 0 时返回 0.0（新流程尚无
        判定时不应除零）。
        """
        stmt = select(GateAudit.decision).where(
            GateAudit.process_id == process_id,
            GateAudit.created_at >= since,
            GateAudit.shadowed.is_(False),
        )
        async with self._session_factory() as db:
            decisions = list((await db.execute(stmt)).scalars().all())
        total = len(decisions)
        if total == 0:
            return 0.0
        return sum(1 for d in decisions if d == "degrade") / total

    async def unchecked_declared_rules(self, process_id: str, since: datetime) -> list[dict]:
        """用途三：核对 `checked_rules` 是否覆盖该流程 redlines 声明的全部规则 id。

        8.5 第三条复盘用途："声明了哪些"来自 `process_redline` 该流程各版本的 `rule_ids`
        并集，"执行了哪些"来自 `gate_audit.checked_rules` 并集，差集即"声明了但没执行"的
        配置错误（对应 T2-43 对两种"未覆盖"的区分）。按 `config_version` 分组返回，
        使配置变更的回归验证不必自己按版本切分。
        """
        declared_stmt = select(ProcessRedlineRow.config_version, ProcessRedlineRow.rule_ids).where(
            ProcessRedlineRow.process_id == process_id
        )
        checked_stmt = select(GateAudit.config_version, GateAudit.checked_rules).where(
            GateAudit.process_id == process_id,
            GateAudit.created_at >= since,
        )
        async with self._session_factory() as db:
            declared_rows = (await db.execute(declared_stmt)).all()
            checked_rows = (await db.execute(checked_stmt)).all()

        declared: dict[int, set[str]] = {}
        for version, rule_ids in declared_rows:
            declared.setdefault(version, set()).update(rule_ids or [])
        checked: dict[int, set[str]] = {}
        for version, rule_ids in checked_rows:
            checked.setdefault(version, set()).update(rule_ids or [])

        report: list[dict] = []
        for version in sorted(declared):
            missing = sorted(declared[version] - checked.get(version, set()))
            if missing:
                report.append(
                    {
                        "config_version": version,
                        "declared": sorted(declared[version]),
                        "checked": sorted(checked.get(version, set())),
                        "missing": missing,
                    }
                )
        return report


async def enqueue_review_ticket(
    *,
    process_id: str,
    config_version: int,
    rule_ids: list[str],
    knowledge_ids: list[str],
    decision: str,
    session_factory: _SessionFactory | None = None,
) -> GateReviewTicket:
    """block 事件的知识条目复核工单入队（T2-46 / T2-45）。

    直接写工单表：本项依赖 T1-13 的发件箱仅为"若可用则走发件箱"的可选路径，而复核工单
    需要被运营侧**立即**看到，走发件箱会引入投递延迟且增加"事件已投递但工单未建"的中间态。
    入参用关键字形式：`process_id` 与 `config_version` 都是短字符串/整数，顺序参数极易传反，
    单测里也因关键字而无法察觉。
    """
    row = GateReviewTicket(
        id=str(uuid4()),
        process_id=process_id,
        config_version=config_version,
        rule_ids=list(rule_ids),
        knowledge_ids=list(knowledge_ids),
        decision=decision,
        status=STATUS_PENDING,
    )
    factory = session_factory or SessionLocal
    async with factory() as db:
        db.add(row)
        await db.commit()
    return row


__all__ = ["GateAuditRepository", "enqueue_review_ticket"]
