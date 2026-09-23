"""基线：按现有 ORM 模型建立全部业务表

Revision ID: 0001
Revises: 0001_ext
Create Date: 2026-09-23

与 T1-03 的衔接：`0001_ext` 建扩展与 knowledge / checkpoint schema，本支在其
之后建 11 张业务表（`public` schema），`0002_env_reading` 再在本支之后建超表。
因此链为 0001_ext -> 0001 -> 0002_env_reading，唯一 head 是 0002_env_reading。

替换掉的旧迁移：`0001_initial.py`（只调用 ORM 元数据的建表方法，把 ORM 定义
当表结构真相）与 `0002_broadcast.py`（用 inspector 猜列是否存在）。两者都让
首次迁移必然成功，却让后续字段变更不可追溯。

autogenerate 漏掉、本文件手工补齐的四类：
1. 主键长度统一为 String(36)（全局约定要求）。
2. 唯一约束：`user.openid`、`device.device_sn`、`farmer_profile.user_id`、
   `user_profile_tag.user_id`。
3. 高优先级外键：`ai_message -> ai_conversation`、`farmer_profile -> user`。
4. T1-06 / T1-11 的性能前提索引：`ai_conversation.update_time`、
   `ai_message.conversation_id`、各表 `user_id`。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = "0001_ext"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    """create_time / update_time：统一 TIMESTAMPTZ + now() 默认值。"""
    return [
        sa.Column(
            "create_time",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "update_time",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="更新时间",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.String(36), primary_key=True, comment="用户ID"),
        sa.Column("openid", sa.String(64), nullable=False, comment="微信openid"),
        sa.Column("unionid", sa.String(64), nullable=True, comment="微信unionid"),
        sa.Column("nick_name", sa.String(64), nullable=True, comment="昵称"),
        sa.Column("avatar_url", sa.String(512), nullable=True, comment="头像URL"),
        sa.Column("gender", sa.SmallInteger(), nullable=False, comment="0未知1男2女"),
        sa.Column("country", sa.String(32), nullable=True),
        sa.Column("province", sa.String(32), nullable=True),
        sa.Column("city", sa.String(32), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_user_openid", "user", ["openid"], unique=True)

    op.create_table(
        "farmer_profile",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("real_name", sa.String(32), nullable=False, comment="真实姓名"),
        sa.Column("age", sa.SmallInteger(), nullable=True),
        sa.Column("gender", sa.SmallInteger(), nullable=False, comment="1男2女"),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("experience_year", sa.SmallInteger(), nullable=False, comment="种植经验年限"),
        sa.Column("province", sa.String(32), nullable=True),
        sa.Column("city", sa.String(32), nullable=True),
        sa.Column("district", sa.String(32), nullable=True),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("total_mu", sa.Numeric(8, 2), nullable=False, comment="总种植面积(亩)"),
        sa.Column("cultivation_type", sa.SmallInteger(), nullable=False, comment="1露地2大棚3混合"),
        sa.Column("water_source", sa.String(64), nullable=True),
        sa.Column("remark", sa.String(500), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_farmer_profile_user_id", "farmer_profile", ["user_id"], unique=True)

    op.create_table(
        "crop",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("field_id", sa.String(36), nullable=True, comment="关联地块ID"),
        sa.Column("crop_name", sa.String(32), nullable=False, comment="作物名称(用户自由输入)"),
        sa.Column("crop_variety", sa.String(64), nullable=True, comment="品种"),
        sa.Column("sowing_date", sa.Date(), nullable=True),
        sa.Column("growth_stage", sa.String(32), nullable=True, comment="生长阶段"),
        sa.Column("plant_mu", sa.Numeric(8, 2), nullable=False, comment="种植面积(亩)"),
        sa.Column("is_main", sa.SmallInteger(), nullable=False, comment="1主作物0否"),
        sa.Column("status", sa.SmallInteger(), nullable=False, comment="1在种0已收获"),
        sa.Column("harvest_date", sa.Date(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_crop_user_id", "crop", ["user_id"])

    op.create_table(
        "field",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False, comment="地块名称"),
        sa.Column("mu", sa.Numeric(8, 2), nullable=False, comment="面积(亩)"),
        sa.Column("soil_type", sa.String(32), nullable=True, comment="土壤类型"),
        sa.Column("soil_ph", sa.Numeric(3, 1), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 6), nullable=True),
        sa.Column("latitude", sa.Numeric(10, 6), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_field_user_id", "field", ["user_id"])

    op.create_table(
        "device",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=True, comment="未绑定为null"),
        sa.Column("field_id", sa.String(36), nullable=True),
        sa.Column("device_sn", sa.String(64), nullable=False, comment="设备序列号"),
        sa.Column("device_type", sa.String(32), nullable=False, comment="soil/weather/camera"),
        sa.Column("device_name", sa.String(64), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, comment="0离线1在线2故障"),
        sa.Column("last_online", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bind_time", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_device_user_id", "device", ["user_id"])
    op.create_index("ix_device_device_sn", "device", ["device_sn"], unique=True)

    op.create_table(
        "location",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("location_name", sa.String(128), nullable=False, comment="地点名称"),
        sa.Column("latitude", sa.Numeric(10, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 6), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, comment="manual/gps"),
        *_timestamps(),
    )
    op.create_index("ix_location_user_id", "location", ["user_id"])

    op.create_table(
        "broadcast_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("date", sa.String(16), nullable=False, comment="YYYY-MM-DD"),
        sa.Column("time", sa.String(8), nullable=True),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("content", sa.String(500), nullable=False),
        sa.Column("type", sa.String(32), nullable=False, comment="fertilize/pest/water/info"),
        sa.Column("level", sa.String(10), nullable=False, comment="danger/warning/normal"),
        sa.Column("source", sa.String(20), nullable=False, comment="sensor/weather/calendar/ai"),
        sa.Column("meta", postgresql.JSONB(), nullable=True, comment="触发该条播报的环境快照"),
        sa.Column("done", sa.SmallInteger(), nullable=False, comment="0未完成1已完成"),
        *_timestamps(),
    )
    op.create_index("ix_broadcast_log_user_id", "broadcast_log", ["user_id"])
    op.create_index("ix_broadcast_log_date", "broadcast_log", ["date"])

    op.create_table(
        "ai_conversation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(128), nullable=False, comment="对话标题"),
        sa.Column("crop_tag", sa.String(32), nullable=True, comment="作物标签"),
        *_timestamps(),
    )
    op.create_index("ix_ai_conversation_user_id", "ai_conversation", ["user_id"])
    # T1-11 的历史列表排序前提
    op.create_index("ix_ai_conversation_update_time", "ai_conversation", ["update_time"])

    op.create_table(
        "ai_message",
        # BigInteger 而非 Integer：消息表增长最快，21 亿上限在 5 年内可能触及
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, comment="user/ai"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("image_url", sa.String(512), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_ai_message_conversation_id", "ai_message", ["conversation_id"])

    op.create_table(
        "memory_summary",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True, comment="会话摘要"),
        sa.Column("key_facts", postgresql.JSONB(), nullable=True, comment="抽取的关键事实"),
        *_timestamps(),
    )
    op.create_index("ix_memory_summary_user_id", "memory_summary", ["user_id"])

    op.create_table(
        "user_profile_tag",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=True, comment="画像标签"),
        sa.Column("context", postgresql.JSONB(), nullable=True, comment="AI上下文快照"),
        sa.Column("last_chat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chat_count", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_user_profile_tag_user_id", "user_profile_tag", ["user_id"], unique=True)

    # 高优先级外键（技术方案 12.7）。其余新增表的外键由各自任务负责。
    op.create_foreign_key(
        "fk_ai_message_conversation",
        "ai_message",
        "ai_conversation",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_farmer_profile_user",
        "farmer_profile",
        "user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # 逆依赖序删除：子表在前，否则外键会阻塞 DROP
    for table in (
        "user_profile_tag",
        "memory_summary",
        "ai_message",
        "ai_conversation",
        "broadcast_log",
        "location",
        "device",
        "field",
        "crop",
        "farmer_profile",
        "user",
    ):
        op.drop_table(table)
