"""T5-01 验收：`device_config` / `field_mapping` 的列、索引、外键、唯一约束与归属校验。

跑在真实 PostgreSQL 上：外键级联、唯一约束违例与 `TIMESTAMPTZ` 还原都不是 SQLite 能力范围
内的对象。两张表由迁移 `0014_device_config_mapping` 建出（`migrated_db`），因此本用例同时
验证了"迁移与 ORM 一致"这一前提。

覆盖任务文件点名的断言：两表关键列逐字存在、`field_mapping.device_id` 外键 `ON DELETE CASCADE`、
唯一约束 `uq_field_mapping_device_raw` 生效、`(device_id='d1', raw_field='t1')` 第二条抛
`UniqueViolation`、删除设备行后映射被级联清除，以及三条归属用例。
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.core.ownership import OwnershipError
from app.models import DeviceConfig, FieldMapping
from app.repositories.device import device_config_repo, field_mapping_repo
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("requires_postgres")


@pytest.fixture()
def sync_engine(migrated_db):
    """psycopg 同步引擎：`inspect` 与 `pg_constraint` 查询都是同步 API。"""
    eng = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


@pytest.fixture()
def sync_inspector(sync_engine):
    return inspect(sync_engine)


def _device_config(**overrides) -> DeviceConfig:
    """一条合法的设备配置，逐项覆盖时只改关心的字段。"""
    base = {
        "id": "d1",
        "user_id": "u1",
        "device_sn": "SN-0001",
        "protocol_type": "http_direct",
    }
    base.update(overrides)
    return DeviceConfig(**base)


def _mapping(**overrides) -> FieldMapping:
    """一条合法的字段映射，逐项覆盖时只改关心的字段。"""
    base = {
        "id": "m1",
        "user_id": "u1",
        "device_id": "d1",
        "raw_field": "t1",
        "standard_metric": "soil_temp",
        "unit": "°C",
    }
    base.update(overrides)
    return FieldMapping(**base)


async def _insert(session_factory, *objs: object) -> None:
    async with session_factory() as db:
        for obj in objs:
            db.add(obj)
        await db.commit()


# ---- 列与结构（连库） ----


def test_device_config_columns(sync_inspector) -> None:
    """`device_config` 含方案 12.6 的全部列，逐字一致。"""
    cols = {c["name"] for c in sync_inspector.get_columns("device_config")}
    expected = {
        "id",
        "user_id",
        "device_sn",
        "farm_entry_id",
        "vendor_template_id",
        "protocol_type",
        "online_status",
        "last_report_at",
        "bound_at",
        "unbound_at",
        "secret_ref",
        "secret_digest",
    }
    assert expected <= cols


def test_field_mapping_columns(sync_inspector) -> None:
    """`field_mapping` 含方案 12.6 的全部列，逐字一致。"""
    cols = {c["name"] for c in sync_inspector.get_columns("field_mapping")}
    expected = {
        "id",
        "user_id",
        "device_id",
        "raw_field",
        "standard_metric",
        "unit",
        "scale_factor",
        "offset_value",
        "data_type",
        "sample_raw",
    }
    assert expected <= cols


def test_indexes_exist(sync_inspector) -> None:
    """任务文件的索引清单逐条落地（含 `device_sn` 唯一索引与在绑复合索引）。"""
    cfg_ix = {i["name"] for i in sync_inspector.get_indexes("device_config")}
    map_ix = {i["name"] for i in sync_inspector.get_indexes("field_mapping")}

    assert {"ix_device_config_user_id", "ix_device_config_device_sn", "ix_device_config_user_bound"} <= cfg_ix
    assert {"ix_field_mapping_user_id", "ix_field_mapping_device"} <= map_ix

    # device_sn 唯一性由唯一索引承载（列级 unique=True, index=True）。
    sn_ix = next(i for i in sync_inspector.get_indexes("device_config") if i["name"] == "ix_device_config_device_sn")
    assert sn_ix["unique"] is True


def test_field_mapping_fk_is_cascade(sync_engine) -> None:
    """`field_mapping.device_id` 外键指向 `device_config` 且 `ON DELETE CASCADE`。"""
    with sync_engine.connect() as conn:
        fk = conn.execute(
            text(
                "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
                "WHERE contype='f' AND conname='field_mapping_device_id_fkey'"
            )
        ).one()
    assert fk[0] == "device_config"
    assert fk[1] == "c"  # c = CASCADE


# ---- 约束与违例（连库） ----


async def test_field_mapping_unique_constraint(session_factory) -> None:
    """同一设备同一原始字段的第二条映射必须被唯一约束拦下。"""
    await _insert(session_factory, _device_config(), _mapping())
    with pytest.raises(IntegrityError):
        await _insert(session_factory, _mapping(id="m2"))


async def test_device_sn_unique_constraint(session_factory) -> None:
    """同一 `device_sn` 的第二条设备配置必须被唯一约束拦下。"""
    await _insert(session_factory, _device_config())
    with pytest.raises(IntegrityError):
        await _insert(session_factory, _device_config(id="d2", user_id="u2"))


async def test_cascade_delete_removes_mappings(session_factory) -> None:
    """物理删除设备行后其映射被级联清除（软删除解绑不走此路径，T5-01 第五节）。"""
    await _insert(
        session_factory,
        _device_config(),
        _mapping(id="m1", raw_field="t1"),
        _mapping(id="m2", raw_field="h1", standard_metric="soil_moisture"),
    )
    async with session_factory() as db:
        await db.execute(text("DELETE FROM device_config WHERE id = 'd1'"))
        await db.commit()
        remaining = (await db.execute(select(FieldMapping))).scalars().all()
    assert remaining == []


async def test_protocol_type_and_scale_defaults(session_factory) -> None:
    """`protocol_type` 默认 `http_direct`，`scale_factor` / `offset_value` 默认 1 / 0。"""
    await _insert(session_factory, _device_config(), _mapping(scale_factor=None, offset_value=None, unit=None))
    async with session_factory() as db:
        cfg = (await db.execute(select(DeviceConfig).where(DeviceConfig.id == "d1"))).scalar_one()
        mapping = (await db.execute(select(FieldMapping).where(FieldMapping.id == "m1"))).scalar_one()
    assert cfg.protocol_type == "http_direct"
    assert float(mapping.scale_factor) == 1.0
    assert float(mapping.offset_value) == 0.0


async def test_bound_at_has_tz_and_server_default(session_factory) -> None:
    """`bound_at` 不显式给值时由数据库 `now()` 兜底，且带时区。"""
    await _insert(session_factory, _device_config())
    async with session_factory() as db:
        cfg = (await db.execute(select(DeviceConfig).where(DeviceConfig.id == "d1"))).scalar_one()
    assert cfg.bound_at is not None
    assert cfg.bound_at.tzinfo is not None
    assert cfg.bound_at <= datetime.now(timezone.utc)


# ---- 归属用例（连库） ----


async def test_device_config_cross_user_read_returns_none(session_factory) -> None:
    """`get(db, "u2", "d1")` 对属于 `u1` 的设备返回 `None`。"""
    await _insert(session_factory, _device_config())
    async with session_factory() as db:
        assert await device_config_repo.get(db, "u2", "d1") is None
        assert await device_config_repo.get(db, "u1", "d1") is not None


async def test_field_mapping_cross_user_list_returns_empty(session_factory) -> None:
    """`list_for_device(db, "u2", "d1")` 对 `u1` 的映射返回空列表。"""
    await _insert(session_factory, _device_config(), _mapping())
    async with session_factory() as db:
        assert await field_mapping_repo.list_for_device(db, "u2", "d1") == []
        assert len(await field_mapping_repo.list_for_device(db, "u1", "d1")) == 1


async def test_field_mapping_user_mismatch_rejected_by_repo(session_factory) -> None:
    """`field_mapping.user_id` 与当前用户不一致时写入被仓储层拒绝。"""
    async with session_factory() as db:
        with pytest.raises(OwnershipError):
            await field_mapping_repo.add(db, "u1", _mapping(user_id="u2"))


async def test_device_config_get_by_sn_ignores_unbound(session_factory) -> None:
    """`get_by_sn` 只命中在绑设备；`get_by_sn_any` 两者都能命中。"""
    await _insert(session_factory, _device_config())
    async with session_factory() as db:
        assert await device_config_repo.get_by_sn(db, "SN-0001") is not None
        assert await device_config_repo.get_by_sn_any(db, "SN-0001") is not None
        await device_config_repo.update_fields(db, "u1", "d1", unbound_at=datetime(2026, 10, 12, tzinfo=timezone.utc))
        await db.commit()
        assert await device_config_repo.get_by_sn(db, "SN-0001") is None
        assert await device_config_repo.get_by_sn_any(db, "SN-0001") is not None


__all__ = ["sync_engine", "sync_inspector"]
