"""T2-07 验收：流程配置表、红线表与 seeds 同步的幂等性。

跑在真实 PostgreSQL 上：两张表依赖 `JSONB`、`DISTINCT ON` 与 GIN 索引，SQLite 无法承载。
表由迁移建出（`migrated_db`），因此这里同时验证了"迁移与 ORM 一致"这一前提。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.agent.state import ProcessId
from app.models import ProcessConfigRow, ProcessRedlineRow
from app.process_config.loader import load_all_configs
from app.process_config.models import ProcessConfig
from app.process_config.version import compute_config_hash, compute_payload_hash
from app.repositories.process_config import ProcessConfigRepository
from app.services.config_sync_service import (
    ACTION_INSERTED,
    ACTION_SKIPPED,
    ACTION_WOULD_INSERT,
    ConfigSyncService,
)

pytestmark = pytest.mark.usefixtures("requires_postgres")

_FLAGSHIP = ProcessId.TEA


def _config(version: int) -> ProcessConfig:
    """取一份真实配置并把版本号改成指定值，用于构造两个版本。"""
    return load_all_configs()[_FLAGSHIP].model_copy(update={"version": version})


async def _count(session_factory, model) -> int:
    async with session_factory() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _insert_version(session_factory, config: ProcessConfig) -> None:
    """建一个版本并提交。`insert_version` 与 `insert_redlines` 不自行提交，由调用方决定。"""
    async with session_factory() as db:
        repo = ProcessConfigRepository(db)
        await repo.insert_version(config)
        await repo.insert_redlines(config)
        await db.commit()


async def test_insert_two_versions_keeps_single_active(session_factory) -> None:
    """插入两个版本后：`latest` 返回版本较高者且其 `effective_to` 为 NULL，旧版本非空。"""
    await _insert_version(session_factory, _config(1))
    await _insert_version(session_factory, _config(2))

    async with session_factory() as db:
        repo = ProcessConfigRepository(db)
        latest = await repo.latest(_FLAGSHIP)
        old = await repo.by_version(_FLAGSHIP, 1)

    assert latest is not None and old is not None
    assert latest.config_version == 2
    assert latest.effective_to is None
    assert old.effective_to is not None
    # 区间首尾相接：旧的结束时刻等于新的开始时刻，不留空隙也不重叠。
    assert old.effective_to == latest.effective_from


async def test_only_one_active_row_per_process(session_factory) -> None:
    """任意时刻至多一条 `effective_to IS NULL`，这是 `latest` 确定性的前提。"""
    await _insert_version(session_factory, _config(1))
    await _insert_version(session_factory, _config(2))
    await _insert_version(session_factory, _config(3))

    async with session_factory() as db:
        active = (
            (
                await db.execute(
                    select(ProcessConfigRow).where(
                        ProcessConfigRow.process_id == _FLAGSHIP.value,
                        ProcessConfigRow.effective_to.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(active) == 1
    assert active[0].config_version == 3


async def test_by_version_returns_any_history(session_factory) -> None:
    """`by_version` 能取回任一历史版本，这是历史问诊复现的入口。"""
    await _insert_version(session_factory, _config(1))
    await _insert_version(session_factory, _config(2))

    async with session_factory() as db:
        repo = ProcessConfigRepository(db)
        first = await repo.by_version(_FLAGSHIP, 1)
        second = await repo.by_version(_FLAGSHIP, 2)
        missing = await repo.by_version(_FLAGSHIP, 99)

    assert first is not None and first.config_version == 1
    assert second is not None and second.config_version == 2
    assert missing is None


async def test_latest_per_process_returns_active_rows(session_factory) -> None:
    """`latest_per_process` 只返回当前生效版本，供 T2-05 的启动引导消费。"""
    await _insert_version(session_factory, _config(1))
    await _insert_version(session_factory, _config(2))

    async with session_factory() as db:
        rows = await ProcessConfigRepository(db).latest_per_process()

    assert set(rows) == {_FLAGSHIP}
    assert rows[_FLAGSHIP].config_version == 2


async def test_config_json_roundtrips_and_hash_matches(session_factory) -> None:
    """落库的 `config_json` 能还原为 `ProcessConfig`，且 `content_hash` 与重算一致。"""
    await _insert_version(session_factory, _config(1))

    async with session_factory() as db:
        row = await ProcessConfigRepository(db).latest(_FLAGSHIP)

    assert row is not None
    restored = ProcessConfig.model_validate(row.config_json)
    assert restored.process_id is _FLAGSHIP
    assert restored == _config(1)
    # 两条路径都必须与入库时的哈希一致：一条按对象算，一条按已落库的 JSON 算。
    assert compute_config_hash(restored) == row.content_hash
    assert compute_payload_hash(row.config_json) == row.content_hash


async def test_insert_version_writes_named_redlines(session_factory) -> None:
    """红线独立落表，可按版本与规则 id 反查（GIN 索引上的一次简单查询）。"""
    config = _config(1)
    await _insert_version(session_factory, config)

    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(ProcessRedlineRow)
                    .where(ProcessRedlineRow.process_id == _FLAGSHIP.value)
                    .where(ProcessRedlineRow.config_version == 1)
                )
            )
            .scalars()
            .all()
        )
        # JSONB 数组包含查询：正是 `ix_process_redline_rule` 要服务的形态
        by_rule = (
            (
                await db.execute(
                    select(ProcessRedlineRow.process_id)
                    .where(ProcessRedlineRow.rule_ids.contains(["R-INTERVAL"]))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == len(config.redlines)
    assert {r.redline_id for r in rows} == {r.id for r in config.redlines}
    assert all(isinstance(r.rule_ids, list) and r.rule_ids for r in rows)
    assert all(r.severity in {"block", "warn"} for r in rows)
    assert _FLAGSHIP.value in by_rule


async def test_insert_version_rejects_duplicate_version(session_factory) -> None:
    """唯一约束 `uq_process_config_version` 生效：同流程同版本不可重复插入。"""
    from sqlalchemy.exc import IntegrityError

    await _insert_version(session_factory, _config(1))

    with pytest.raises(IntegrityError):
        async with session_factory() as db:
            repo = ProcessConfigRepository(db)
            # 绕过 `insert_version` 的"封旧"步骤直接插同版本，验证约束本身
            db.add(
                ProcessConfigRow(
                    id=str(uuid4()),
                    process_id=_FLAGSHIP.value,
                    config_version=1,
                    display_name="重复版本",
                    config_json={},
                    content_hash="x" * 12,
                    source="manual",
                    effective_from=datetime.now(UTC),
                )
            )
            await db.commit()


async def test_sync_twice_is_idempotent(session_factory) -> None:
    """连续两次同步：第一次全部 inserted，第二次全部 skip，且行数不变。"""
    async with session_factory() as db:
        service = ConfigSyncService(db)
        first = await service.sync_from_seeds()
        await db.commit()

    assert len(first) == len(load_all_configs())
    assert all(r.action == ACTION_INSERTED for r in first)
    rows_after_first = await _count(session_factory, ProcessConfigRow)

    async with session_factory() as db:
        service = ConfigSyncService(db)
        second = await service.sync_from_seeds()
        await db.commit()

    assert all(r.action == ACTION_SKIPPED for r in second)
    assert await _count(session_factory, ProcessConfigRow) == rows_after_first


async def test_sync_dry_run_writes_nothing(session_factory) -> None:
    """dry-run 全部返回 `would_insert`，且不产生任何数据库写入。"""
    async with session_factory() as db:
        service = ConfigSyncService(db)
        reports = await service.sync_from_seeds(dry_run=True)
        await db.commit()

    assert reports and all(r.action == ACTION_WOULD_INSERT for r in reports)
    assert all(r.version is not None for r in reports)
    assert await _count(session_factory, ProcessConfigRow) == 0
    assert await _count(session_factory, ProcessRedlineRow) == 0


async def test_sync_bumps_version_beyond_yaml_value(session_factory, monkeypatch) -> None:
    """内容变化时自增版本，而不是沿用 YAML 里的手写版本号。

    用 monkeypatch 替换 `load_all_configs`：这是唯一能在不改 YAML 的前提下模拟
    「内容变了但手写版本号没变」的方式，而那正是幂等判据必须覆盖的情形。
    """
    from app.services import config_sync_service

    original = load_all_configs()
    async with session_factory() as db:
        await ConfigSyncService(db).sync_from_seeds()
        await db.commit()

    changed = {
        pid: cfg.model_copy(update={"display_name": f"{cfg.display_name}（修订）"}) for pid, cfg in original.items()
    }
    monkeypatch.setattr(config_sync_service, "load_all_configs", lambda: changed)

    async with session_factory() as db:
        reports = await ConfigSyncService(db).sync_from_seeds()
        await db.commit()

    # YAML 侧版本号仍是 1，但库里必须落到 2：版本序列由系统维护。
    assert all(r.action == ACTION_INSERTED for r in reports)
    assert all(r.version == 2 for r in reports)

    async with session_factory() as db:
        latest = await ProcessConfigRepository(db).latest(_FLAGSHIP)
    assert latest is not None and latest.config_version == 2
    assert compute_config_hash(ProcessConfig.model_validate(latest.config_json)) == latest.content_hash
