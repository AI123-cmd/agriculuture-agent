"""T1-03 验收：扩展、schema、超表与向量列。

这些断言都跑在真实 PostgreSQL 上（由 `engine` fixture 提供，T1-04 起该 fixture
用 `alembic upgrade head` 建库），SQLite 无法覆盖 `vector` 类型与 `create_hypertable`。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("requires_postgres")


async def test_required_extensions_installed(engine) -> None:
    """vector 与 timescaledb 两个扩展各存在一行。"""
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'timescaledb')")))
            .scalars()
            .all()
        )

    assert set(rows) == {"vector", "timescaledb"}


async def test_knowledge_and_checkpoint_schemas_exist(engine) -> None:
    """knowledge 与 checkpoint schema 必须已建。

    schema 缺失时 PostgreSQL 会静默忽略 search_path 项、把表落到 public，
    因此这里显式断言，避免"看起来成功、实际错位"。
    """
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(text("SELECT nspname FROM pg_namespace WHERE nspname IN ('knowledge', 'checkpoint')")))
            .scalars()
            .all()
        )

    assert set(rows) == {"knowledge", "checkpoint"}


async def test_env_reading_is_hypertable(engine) -> None:
    """env_reading 已注册为 TimescaleDB 超表，且只注册一次。"""
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'env_reading'")
            )
        ).scalar_one()

    assert count == 1


async def test_env_reading_primary_key_is_composite(engine) -> None:
    """主键必须是 (id, observed_at) 两列。单列主键在超表上根本建不出来。"""
    async with engine.connect() as conn:
        columns = (
            (
                await conn.execute(
                    text(
                        "SELECT a.attname FROM pg_index i "
                        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                        "WHERE i.indrelid = 'env_reading'::regclass AND i.indisprimary "
                        "ORDER BY a.attname"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert sorted(columns) == ["id", "observed_at"]


async def test_embedding_column_is_vector_type(engine) -> None:
    """knowledge_item.embedding 的 udt_name 为 vector。"""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_schema = 'knowledge' AND table_name = 'knowledge_item' "
                    "AND column_name = 'embedding'"
                )
            )
        ).scalar_one_or_none()

    assert row == "vector"


async def test_knowledge_item_embedding_accepts_1024_dim_vector(engine) -> None:
    """向量列可写入并可按距离算子比较（维度 1024，与 Settings.EMBEDDING_DIM 一致）。"""
    from app.core.config import get_settings

    assert get_settings().EMBEDDING_DIM == 1024

    # 用非零向量：pgvector 的余弦距离 `<=>` 对零向量未定义，返回 NaN
    vec = str([1.0] * 1024)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO knowledge.knowledge_item "
                "(knowledge_id, process_id, kind, status, title, content, registered_crops, forbidden_combinations, embedding) "
                "VALUES ('K-1023', 'rice', 'pest', 'approved', 't', 'c', '[]'::jsonb, '[]'::jsonb, :vec)"
            ),
            {"vec": vec},
        )
        similarity = (
            await conn.execute(
                text("SELECT 1 - (embedding <=> :probe) FROM knowledge.knowledge_item WHERE knowledge_id = 'K-1023'"),
                {"probe": vec},
            )
        ).scalar_one()
        await conn.execute(text("DELETE FROM knowledge.knowledge_item WHERE knowledge_id = 'K-1023'"))

    assert similarity == pytest.approx(1.0, abs=1e-6)


async def test_insert_env_reading(engine) -> None:
    """插入一条读数成功，计数为 1。"""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO env_reading (user_id, metric, value, source, observed_at, quality) "
                "VALUES ('u1', 'soil_temp', 22.5, 'sensor', now(), 'ok')"
            )
        )
        count = (await conn.execute(text("SELECT count(*) FROM env_reading"))).scalar_one()
        await conn.execute(text("DELETE FROM env_reading"))

    assert count == 1


async def test_seven_day_chunk_interval_takes_effect(engine) -> None:
    """跨 30 天写入读数后 chunk 数超过 4，证明 7 天分区生效。

    30 天 / 7 天 ≈ 5，落在不同分区的行会各自建 chunk，故必然多于 4 个。
    """
    now = datetime.now(UTC)
    stamps = [now - timedelta(days=offset) for offset in range(1, 36)]

    async with engine.begin() as conn:
        for stamp in stamps:
            await conn.execute(
                text(
                    "INSERT INTO env_reading (user_id, metric, value, source, observed_at, quality) "
                    "VALUES ('u1', 'soil_temp', 22.5, 'sensor', :ts, 'ok')"
                ),
                {"ts": stamp},
            )
        chunk_count = (
            await conn.execute(
                text("SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'env_reading'")
            )
        ).scalar_one()
        await conn.execute(text("DELETE FROM env_reading"))

    assert chunk_count > 4
