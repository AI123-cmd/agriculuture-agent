"""seeds 到数据库的配置同步任务（T2-07）。

数据库成为配置的运行时事实来源之后，seeds 只在首次部署时作为初始数据源；本服务是
把 YAML 推进数据库的唯一入口，也可供后台在提交人工修订后调用。

幂等靠 `content_hash` 比对实现，而不是比对 `version`。原因：YAML 里的 `version` 是
编写者手写的，容易忘记递增；若按它判断，「改了内容但没改版本号」会被判为无需同步，
线上就一直是旧配置。数据库里的 `config_version` 由本服务自增，是真正的版本序列，
因此「更新配置」这个动作只需改 YAML，版本号由系统维护。

`dry_run` 是必要的：CI 与预发环境先跑一次看会插入几条，避免误改一次配置就在生产
插入一堆版本。它必须在写库之前返回，不能靠「跑完再回滚」模拟——回滚会消耗序列并可能
留下副作用。

本服务**不提交事务**：是否提交由调用方决定（请求路径由 `get_db` 兜底回滚，定时任务
自行提交），这样「部分流程同步成功」可以被整体回滚，不会留下半同步的库。
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import ProcessId
from app.models.process_config import SOURCE_SEED
from app.process_config.loader import load_all_configs
from app.process_config.version import compute_config_hash
from app.repositories.process_config import ProcessConfigRepository

logger = logging.getLogger(__name__)

# 同步动作三值。`skip` 与 `would_insert` 分开：前者表示内容未变，后者表示 dry-run
# 下本应写入。二者都"什么都没写"，但排查时结论完全不同，不能合成一个值。
ACTION_INSERTED = "inserted"
ACTION_SKIPPED = "skip"
ACTION_WOULD_INSERT = "would_insert"
ACTION_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SyncReport:
    """单条流程的同步结果。"""

    process_id: ProcessId
    action: str
    version: int | None = None
    reason: str | None = None


class ConfigSyncService:
    """把 seeds 或人工提交的配置同步到数据库。幂等：内容未变则不写新版本。"""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ProcessConfigRepository(session)

    async def sync_from_seeds(self, *, dry_run: bool = False) -> list[SyncReport]:
        """同步全部可加载的流程配置，返回逐条结果。

        `load_all_configs()` 会跳过加载失败的流程并打 ERROR 日志（不抛异常，避免一条
        坏配置让整轮同步失败）。这些流程在这里补一条 `unavailable` 记录：同步要能回答
        「七条流程各自处于什么状态」，缺一条记录会让「某条流程没被同步」变成看不出来的
        事实。
        """
        configs = load_all_configs()
        reports: list[SyncReport] = []

        for pid in ProcessId:
            config = configs.get(pid)
            if config is None:
                reports.append(SyncReport(pid, ACTION_UNAVAILABLE, reason="配置加载失败"))
                continue

            current = await self._repo.latest(pid)
            incoming_hash = compute_config_hash(config)
            if current is not None and current.content_hash == incoming_hash:
                reports.append(SyncReport(pid, ACTION_SKIPPED, version=current.config_version, reason="内容未变"))
                continue

            # 版本号由数据库序列自增，不用 YAML 里的手写值：手写值在同一台机器上
            # 反复覆盖会被判为"版本回退"，而历史问诊依赖版本号单调递增。
            next_version = current.config_version + 1 if current is not None else config.version
            bumped = config.model_copy(update={"version": next_version})

            if dry_run:
                reports.append(SyncReport(pid, ACTION_WOULD_INSERT, version=next_version, reason="dry-run 未写入"))
                continue

            await self._repo.insert_version(bumped, source=SOURCE_SEED, note="seeds 同步")
            count = await self._repo.insert_redlines(bumped)
            logger.info(
                "process config synced",
                extra={
                    "process_id": pid.value,
                    "version": next_version,
                    "content_hash": incoming_hash,
                    "redlines": count,
                },
            )
            reports.append(SyncReport(pid, ACTION_INSERTED, version=next_version, reason=f"红线 {count} 条"))

        return reports


__all__ = [
    "ACTION_INSERTED",
    "ACTION_SKIPPED",
    "ACTION_UNAVAILABLE",
    "ACTION_WOULD_INSERT",
    "ConfigSyncService",
    "SyncReport",
]
