"""建 env_reading 时序表并转为 TimescaleDB 超表

Revision ID: 0002_env_reading
Revises: 0001
Create Date: 2026-09-23

两个必须照做的细节：
1. 主键是 `(id, observed_at)` 复合主键。TimescaleDB 要求分区列进入主键，
   单列 `id` 主键在超表上会直接报错。
2. `chunk_time_interval` 取 7 天。按日增 1.44 亿点位估算，7 天一个 chunk 约
   10 亿行，落在单 chunk 合理区间；取 1 天会产生过多 chunk，取 30 天会让单个
   chunk 过大影响压缩与查询裁剪。

`quality` 三值（ok / suspect / stale）来自产品说明书 4.4.3 功能点 2：异常数据
应被标记并排除在阈值判断之外，而不是直接丢弃或直接使用。本项只建列。
"""

from alembic import op

revision = "0002_env_reading"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 用 op.execute 原样执行建表 SQL：Alembic 的 op.create_table 不识别
    # create_hypertable，且复合主键 + BIGSERIAL 的写法用 DDL 表达更直白。
    op.execute(
        """
        CREATE TABLE env_reading (
            id            BIGSERIAL,
            user_id       VARCHAR(36)   NOT NULL,
            device_id     VARCHAR(36),
            farm_entry_id VARCHAR(36),
            metric        VARCHAR(30)   NOT NULL,
            value         NUMERIC(12,4) NOT NULL,
            unit          VARCHAR(10),
            source        VARCHAR(20)   NOT NULL,
            observed_at   TIMESTAMPTZ   NOT NULL,
            quality       VARCHAR(10)   NOT NULL,
            created_at    TIMESTAMPTZ   DEFAULT now(),
            PRIMARY KEY (id, observed_at)
        )
        """
    )
    op.execute("SELECT create_hypertable('env_reading', 'observed_at', chunk_time_interval => INTERVAL '7 days')")
    op.execute("CREATE INDEX ON env_reading (user_id, metric, observed_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS env_reading")
