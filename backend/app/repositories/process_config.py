"""流程配置仓储：配置与红线的读写入口。

所有查询经此，不使用 `db.query(Model)`（全局约定第四节 L4）。本仓储与其余仓储的
形态不同——其余仓储由调用方每次传入 `db` 与 `user_id`，而本仓储在构造时绑定会话、
且**不接受 `user_id`**：配置表是全局只读数据，没有归属概念（见
`app/models/process_config.py` 的表注释）。这也是它能直接对接 T2-05 的
`registry.ConfigStore` Protocol 的原因。

`insert_version` 的关键是「先封旧再插新」在同一事务里完成。若拆成两次提交，中间
时刻会出现两个 `effective_to IS NULL` 的版本，`latest` 查询随即变得不确定；同一个
时间戳写 `effective_to` 与 `effective_from`，使版本区间首尾相接，任意时刻至多一条
当前生效版本。

`by_version` 是历史问诊复现的入口：给定 `consultation.config_version` 就能取出当时
的完整配置（技术方案 15.6）。
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import ProcessId
from app.models.process_config import SOURCE_SEED, ProcessConfigRow, ProcessRedlineRow
from app.process_config.models import ProcessConfig
from app.process_config.redlines import RedlineValidator
from app.process_config.version import compute_config_hash


class ProcessConfigRepository:
    """流程配置仓储。构造时绑定会话，方法与 `ConfigStore` Protocol 兼容。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(self, process_id: ProcessId) -> ProcessConfigRow | None:
        """取指定流程当前生效的配置行（`effective_to IS NULL`）。"""
        stmt = (
            select(ProcessConfigRow)
            .where(ProcessConfigRow.process_id == process_id.value)
            .where(ProcessConfigRow.effective_to.is_(None))
            .order_by(ProcessConfigRow.config_version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def latest_per_process(self) -> dict[ProcessId, ProcessConfigRow]:
        """一次取回七条流程各自当前生效的配置行，供启动引导使用。

        `DISTINCT ON (process_id)` 由 `distinct()` + 前缀排序表达：PostgreSQL 要求
        `DISTINCT ON` 的表达式与 `ORDER BY` 的最左列一致，故排序首列必须是 `process_id`，
        第二列才是版本倒序。
        """
        stmt = (
            select(ProcessConfigRow)
            .where(ProcessConfigRow.effective_to.is_(None))
            .order_by(ProcessConfigRow.process_id, ProcessConfigRow.config_version.desc())
            .distinct(ProcessConfigRow.process_id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        # 只映射能解析为 `ProcessId` 的行：库里若存在历史遗留的已下线流程，
        # 让 `ProcessId(...)` 抛异常会比静默丢弃更容易发现。
        return {ProcessId(row.process_id): row for row in rows}

    async def by_version(self, process_id: ProcessId, version: int) -> ProcessConfigRow | None:
        """按版本号取回任意历史版本，供历史问诊复现。"""
        stmt = select(ProcessConfigRow).where(
            ProcessConfigRow.process_id == process_id.value,
            ProcessConfigRow.config_version == version,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def insert_version(
        self,
        config: ProcessConfig,
        *,
        source: str = SOURCE_SEED,
        note: str | None = None,
    ) -> ProcessConfigRow:
        """插入新版本，并把上一版本的 `effective_to` 补齐。同事务内完成。

        `content_hash` 走 `compute_config_hash`（已剔除 `version`）：入库侧的哈希必须与
        同步任务、热加载两侧用同一条路径计算，否则比对永远不等。本方法不做「内容是否
        变化」的判断——那是同步任务（`ConfigSyncService`）的职责，仓储只负责写入。
        """
        now = datetime.now(UTC)
        await self._session.execute(
            update(ProcessConfigRow)
            .where(ProcessConfigRow.process_id == config.process_id.value)
            .where(ProcessConfigRow.effective_to.is_(None))
            .values(effective_to=now)
        )
        row = ProcessConfigRow(
            id=str(uuid4()),
            process_id=config.process_id.value,
            config_version=config.version,
            display_name=config.display_name,
            config_json=config.model_dump(mode="json"),
            content_hash=compute_config_hash(config),
            source=source,
            note=note,
            effective_from=now,
        )
        self._session.add(row)
        return row

    async def insert_redlines(self, config: ProcessConfig) -> int:
        """按配置插入该版本的红线行，返回插入条数。

        红线单独落表而不是塞进 `config_json`，是为了让「哪些流程引用了 `R-INTERVAL`」
        这类反查走 GIN 索引而非 JSON 展开。红线文本由 `RedlineValidator` 展开，
        与加载期用的是同一份实现。
        """
        loaded = RedlineValidator().validate(config)
        for item in loaded.redlines:
            self._session.add(
                ProcessRedlineRow(
                    id=str(uuid4()),
                    process_id=config.process_id.value,
                    config_version=config.version,
                    redline_id=item.id,
                    desc=item.desc,
                    rule_ids=[r.value for r in item.rule_ids],
                    severity=item.severity.value,
                    payload=item.payload,
                )
            )
        return len(loaded.redlines)


__all__ = ["ProcessConfigRepository"]
