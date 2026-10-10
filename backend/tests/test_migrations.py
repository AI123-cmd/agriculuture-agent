"""T1-04 验收：迁移链路真实性、可回滚、与 ORM 元数据一致。

这些用例跑在真实 PostgreSQL 上：迁移里含 `create_hypertable`、`vector(1024)`
与 `JSONB`，SQLite 无法承载，因此统一以 `migrated_db`（会话级迁移到 head）为前提。
"""

import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401  注册全部模型，供元数据比对
from app.core.startup_check import EXPECTED_REVISION
from app.db.base import Base
from tests.conftest import TEST_DATABASE_URL

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 技术方案 12.1 的现有业务表（`broadcast_log` 已由 T3-15 的 0013 迁移废弃，故为 10 张）
BASELINE_TABLES = (
    "user",
    "farmer_profile",
    "crop",
    "field",
    "device",
    "location",
    "ai_conversation",
    "ai_message",
    "memory_summary",
    "user_profile_tag",
)


def _sync_engine():
    """psycopg 同步引擎：compare_metadata 与 inspector 都是同步 API。"""
    url = TEST_DATABASE_URL.replace("+asyncpg", "+psycopg")
    return create_engine(url)


@pytest.fixture()
def sync_engine(migrated_db):
    eng = _sync_engine()
    yield eng
    eng.dispose()


def test_baseline_tables_all_created(sync_engine) -> None:
    """迁移后 public 下 10 张基线业务表全部存在。"""
    names = set(inspect(sync_engine).get_table_names(schema="public"))
    missing = set(BASELINE_TABLES) - names
    assert not missing, f"迁移未建出的表：{sorted(missing)}"


def test_alembic_version_matches_expected_revision(sync_engine) -> None:
    """库中版本号等于代码期望值，避免"迁移加了但忘了更新 EXPECTED_REVISION"。"""
    with sync_engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one()
    assert current == EXPECTED_REVISION


def test_upgrade_head_then_scratch_roundtrip(migrated_db) -> None:
    """upgrade head 与 downgrade -1 交替 3 轮均不报错。"""
    for _ in range(3):
        command.downgrade(migrated_db, "-1")
        command.upgrade(migrated_db, "head")


def test_downgrade_removes_public_tables(sync_engine, migrated_db) -> None:
    """downgrade 到 base 后 10 张基线业务表消失，但扩展不随迁移回滚。"""
    command.downgrade(migrated_db, "base")
    names = set(inspect(sync_engine).get_table_names(schema="public"))
    assert not (set(BASELINE_TABLES) & names)
    with sync_engine.connect() as conn:
        extensions = set(
            conn.execute(text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'timescaledb')"))
            .scalars()
            .all()
        )
    assert extensions == {"vector", "timescaledb"}

    command.upgrade(migrated_db, "head")


def test_metadata_matches_orm(sync_engine) -> None:
    """迁移建出的结构 == ORM 元数据，autogenerate 无差异。

    这一条是"基线迁移人工核对过"的机器化防线：任何漏建的列、索引或约束，
    都会在这里以非空 diff 暴露。

    过滤规则与 alembic/env.py 的 include_name / include_object 保持一致，否则会得到
    大量假差异：
    - `type_ == "schema"` 时 `name` 为 `None` 必须放行：alembic 会把默认 schema
      （public）替换成 `None` 再交给过滤器，拦掉它会让整张 public 被判定为"不存在"；
    - env_reading 是超表、checkpoint_* 由 LangGraph 自建，ORM 元数据无法表达
      create_hypertable 这类改造，比对必然失真，故排除。
    """

    def _include_name(name, type_, parent):
        if type_ == "schema":
            return name is None or name in {"knowledge"}
        if type_ == "table":
            return name != "env_reading" and not (name or "").startswith("checkpoint")
        return True

    def _include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name != "env_reading" and not (name or "").startswith("checkpoint")
        return True

    with sync_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_schemas": True,
                "include_name": _include_name,
                "include_object": _include_object,
            },
        )
        diff = compare_metadata(ctx, Base.metadata)

    assert diff == [], f"迁移与 ORM 元数据不一致：{diff}"


def test_required_indexes_exist(sync_engine) -> None:
    """T1-06 / T1-11 的性能前提索引必须在基线上建出。"""
    inspector = inspect(sync_engine)
    conv_indexes = {ix["name"] for ix in inspector.get_indexes("ai_conversation")}
    msg_indexes = {ix["name"] for ix in inspector.get_indexes("ai_message")}
    crop_indexes = {ix["name"] for ix in inspector.get_indexes("crop")}

    assert "ix_ai_conversation_update_time" in conv_indexes
    assert "ix_ai_message_conversation_id" in msg_indexes
    assert "ix_crop_user_id" in crop_indexes


def test_primary_keys_are_36_chars(sync_engine) -> None:
    """主键统一 String(36)（时序表除外，由 T1-03 用 BIGSERIAL）。"""
    inspector = inspect(sync_engine)
    for table in BASELINE_TABLES:
        if table == "ai_message":
            continue  # BigInteger 自增主键，见 0001_baseline 注释
        pk = inspector.get_pk_constraint(table)["constrained_columns"]
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        for column in pk:
            assert isinstance(cols[column]["type"], sa.String)
            assert cols[column]["type"].length == 36, f"{table}.{column} 主键长度不是 36"


def test_high_priority_foreign_keys_exist(sync_engine) -> None:
    """两条高优先级外键已建（技术方案 12.7）。"""
    inspector = inspect(sync_engine)
    message_fks = {fk["name"] for fk in inspector.get_foreign_keys("ai_message")}
    profile_fks = {fk["name"] for fk in inspector.get_foreign_keys("farmer_profile")}

    assert "fk_ai_message_conversation" in message_fks
    assert "fk_farmer_profile_user" in profile_fks


def test_no_create_all_in_app_and_alembic() -> None:
    """应用与迁移中不得再出现 ORM 元数据建表/删表调用（禁止伪迁移回潮）。"""
    forbidden = ("metadata.create" + "_all", "metadata.drop" + "_all")
    hits: list[str] = []
    for root in (BACKEND_ROOT / "app", BACKEND_ROOT / "alembic"):
        for path in root.rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            if any(token in content for token in forbidden):
                hits.append(str(path.relative_to(BACKEND_ROOT)))
    assert hits == [], f"禁止用 ORM 元数据建表/删表，请改为显式迁移：{hits}"


def test_no_sa_inspect_in_versions() -> None:
    """迁移目录中不得再有"用 inspector 猜列是否存在"的分支。"""
    token = "sa." + "inspect"
    hits = [
        str(path.relative_to(BACKEND_ROOT))
        for path in (BACKEND_ROOT / "alembic" / "versions").rglob("*.py")
        if token in path.read_text(encoding="utf-8")
    ]
    assert hits == [], f"迁移中不得用 inspector 猜列：{hits}"


def test_check_migrations_script_passes() -> None:
    """scripts/check_migrations.sh 返回 0（用 bash 执行）。"""
    script = BACKEND_ROOT / "scripts" / "check_migrations.sh"
    assert script.exists(), "缺少 scripts/check_migrations.sh"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"check_migrations.sh 失败：{result.stdout}\n{result.stderr}"
