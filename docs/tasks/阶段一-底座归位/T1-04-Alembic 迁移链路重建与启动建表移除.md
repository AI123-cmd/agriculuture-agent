# T1-04 Alembic 迁移链路重建与启动建表移除

## 这一项要做什么

现有 `backend/alembic/versions/0001_initial.py` 的 `upgrade()` 只有一行 `Base.metadata.create_all(bind=bind)`，`0002_broadcast.py` 靠 `sa.inspect` 猜列是否存在再 `add_column`。这不是迁移，是"把 ORM 元数据当表结构真相"的障眼法：它让首次迁移必然成功，也让后续任何字段变更不可追溯，最终表现为"开发机能跑、生产缺字段"。本项把迁移链路换成真正的 DDL 序列，删掉启动建表路径，改启动时校验 `alembic_version` 与代码期望一致，不一致则拒绝启动。

## 前置依赖

- 依赖 T1-02 已提供：可用的异步引擎、`DATABASE_URL` 与 `CHECKPOINT_DB_URL`、`psycopg` 驱动。
- 依赖 T1-03 已提供：`vector` / `timescaledb` 扩展、`knowledge` 与 `checkpoint` schema、`env_reading` 超表。
- 外部环境：一个干净空库，`alembic upgrade head` 必须一次成功。
- 需要人工确认：现有 11 张表是否全部保留。技术方案 12.1 要新增的流程配置、知识、处方卡、回访、快照、触达、闸门、发件箱等表**不在本项范围**，各自任务追加迁移。

## 参考章节

- 技术方案 第12章 12.8 迁移策略（重建方式三条）
- 技术方案 第16章 16.2 第一步：底座归位（技术动作 2：迁移链路重建）
- 技术方案 第15章 15.4 测试策略（CI 门禁：`upgrade head` 与 `downgrade -1`）
- 技术方案 第2章 2.1 现有技术栈盘点（迁移链路需重建）
- 产品说明书 9.1 数据层约束、9.4 迁移改用真实迁移链路

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 删除 | `backend/alembic/versions/0001_initial.py` | 移除 `create_all` 伪迁移 |
| 删除 | `backend/alembic/versions/0002_broadcast.py` | 内容并入基线，去掉 `inspect` 分支 |
| 新增 | `backend/alembic/versions/0001_baseline.py` | 全量 `op.create_table` 基线，含索引与约束 |
| 修改 | `backend/alembic/env.py` | 支持 `compare_type` / `compare_server_default` / `include_schemas` |
| 修改 | `backend/alembic.ini` | 设 `file_template`、`version_table_schema` |
| 新增 | `backend/app/core/startup_check.py` | 启动时校验 `alembic_version` 与 `EXPECTED_REVISION` |
| 修改 | `backend/app/main.py` | lifespan 调用 `verify_schema_revision()` |
| 修改 | `backend/tests/conftest.py` | 用迁移建库替代 `Base.metadata.create_all` |
| 新增 | `backend/tests/test_migrations.py` | 断言迁移可执行、可回滚、与 ORM 元数据一致 |
| 新增 | `backend/scripts/check_migrations.sh` | CI 用：空库 upgrade、回滚一步、再前滚 |

## 实现要点

### 一、基线迁移必须人工核对

技术方案 12.8 第一条要求用 `--autogenerate` 生成基线后**人工核对**每个字段的类型、索引、约束。

```powershell
createdb huanong_baseline
alembic revision --autogenerate -m "baseline" --rev-id 0001
# 逐行核对 0001_baseline.py 后再升级
alembic upgrade head
```

autogenerate 会漏四类东西，必须手工补：第一，`server_default` 的时区（依赖 T1-02 的 `DateTime(timezone=True)`）是否生成 `DEFAULT now()`。第二，唯一约束与复合索引常被漏，如 `user.openid`、`device.device_sn`。第三，外键：现状无外键设计，产品说明书 9.4 要求"关键关联补充外键约束"，本项**只在既有表之间补高优先级外键**，新增表的外键各自任务负责。第四，主键长度：现状是 `String(32)`，全局约定要求 `String(36)`，本项统一为 36。

### 二、基线迁移的结构

```python
"""基线：按现有 ORM 模型建立全部业务表

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.String(36), primary_key=True, comment="用户ID"),
        sa.Column("openid", sa.String(64), nullable=False, comment="微信openid"),
        sa.Column("unionid", sa.String(64), nullable=True),
        sa.Column("nick_name", sa.String(64), nullable=True),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column("gender", sa.SmallInteger(), server_default="0"),
        sa.Column("country", sa.String(32), nullable=True),
        sa.Column("province", sa.String(32), nullable=True),
        sa.Column("city", sa.String(32), nullable=True),
        sa.Column("create_time", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_user_openid", "user", ["openid"], unique=True)
    # ... 其余 10 张表逐个 create_table + create_index
    op.create_table(
        "ai_message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("image_url", sa.String(512), nullable=True),
        sa.Column("create_time", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ai_message_conversation_id", "ai_message", ["conversation_id"])
    # 高优先级外键（技术方案 12.7）
    op.create_foreign_key(
        "fk_ai_message_conversation", "ai_message", "ai_conversation",
        ["conversation_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_farmer_profile_user", "farmer_profile", "user",
        ["user_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_ai_conversation_update_time", "ai_conversation", ["update_time"])
    op.create_index("ix_crop_user_id", "crop", ["user_id"])


def downgrade() -> None:
    for table in (
        "user_profile_tag", "memory_summary", "ai_message", "ai_conversation",
        "broadcast_log", "location", "device", "field", "crop", "farmer_profile", "user",
    ):
        op.drop_table(table)
```

`downgrade` 按依赖逆序 `drop_table`，顺序写错会在有外键时失败。`ai_message.id` 从 `Integer` 改 `BigInteger` 是有意的：消息表增长最快，`Integer` 上限 21 亿在 5 年内可能触及。`ix_ai_conversation_update_time` 与 `ix_crop_user_id` 是 T1-11 与 T1-06 的性能前提，必须在基线上建出。

### 三、删除启动建表路径

`Base.metadata.create_all` 现状只出现在 `0001_initial.py`（删除）与 `tests/conftest.py`（改为迁移建库）。加一条 CI 检查防止回潮：

```bash
# scripts/check_migrations.sh 片段
if grep -rn "metadata.create_all" backend/app backend/alembic; then
  echo "禁止在应用与迁移中使用 metadata.create_all，请改为显式迁移"; exit 1
fi
```

`tests/conftest.py` 改为：

```python
@pytest.fixture(scope="session")
def migrated_db():
    """用迁移建测试库，保证迁移链路本身也被测试覆盖。"""
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL_SYNC"])
    command.upgrade(cfg, "head")
    yield
```

测试库从 SQLite 内存改为本地 PG 临时库，因为 `create_hypertable` 与 `vector` 类型在 SQLite 上不存在。这是对技术方案的一处推断，方案未明确测试库形态；CI 无 PG 时退化为 session 级 Docker PG。

### 四、启动时校验迁移版本

```python
# app/core/startup_check.py
EXPECTED_REVISION = "0001"   # 每次新增迁移后由发布流程更新


async def verify_schema_revision(engine) -> None:
    """表结构版本与代码期望不一致时拒绝启动。"""
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT version_num FROM alembic_version"))
        current = row.scalar_one_or_none()
    if current != EXPECTED_REVISION:
        raise RuntimeError(
            f"数据库迁移版本不匹配：数据库为 {current}，代码期望 {EXPECTED_REVISION}，"
            "请先执行 alembic upgrade head"
        )
```

`EXPECTED_REVISION` 与 `alembic heads` 输出的 `alembic/HEAD` 文件双写是刻意的：前者是代码断言值，后者是发布产物校验值，两者都由发布流程刷新，避免"迁移加了但忘了更新期望值"。

### 五、env.py 改造

```python
def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool, future=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,              # 类型变更能被检测
            compare_server_default=True,    # 默认值变更能被检测
            include_schemas=True,           # 覆盖 knowledge / checkpoint schema
            version_table_schema="public",  # 版本表不随 search_path 漂移
        )
        with context.begin_transaction():
            context.run_migrations()
```

`include_schemas=True` 是必需的，否则 `knowledge` schema 下的表不参与 autogenerate 比对。

### 六、检查点表是否纳入迁移

LangGraph 的 `AsyncPostgresSaver.setup()` 会自建检查点表。两条路：让 `setup()` 自建（缺点是检查点表结构不受迁移版本管理），或纳入迁移（缺点是要跟随 LangGraph 版本更新表结构）。本项取折中：`checkpoint` schema 的创建放进基线迁移，表由 `setup()` 管理，启动检查额外断言该 schema 下至少有 4 张表。这是对技术方案的一处推断，7.5 只说"建议独立 schema `checkpoint`"。

### 七、与验收项的对应

- 直接对应产品说明书 9.2 第 7 条（历史列表性能）：基线显式建出 `ai_conversation.update_time` 与 `ai_message.conversation_id` 索引，T1-11 的批量查询才能生效。
- 直接对应产品说明书 9.2 第 8 条（会话回滚）：迁移建库后回滚用例能在真实 PG 上跑，SQLite 的 `SAVEPOINT` 语义差异被消除。
- 对应技术方案 12.8 全部三条要求。

## 验收标准

- 空库上 `alembic upgrade head` 返回 0，且 `SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('user','farmer_profile','crop','field','device','location','broadcast_log','ai_conversation','ai_message','memory_summary','user_profile_tag')` 返回 11。
- `alembic downgrade -1` 返回 0；`upgrade head` 与 `downgrade -1` 交替 3 轮均无错误。
- `SELECT version_num FROM alembic_version` 等于 `alembic heads` 的输出。
- `grep -rn "metadata.create_all\|metadata.drop_all" backend/app backend/alembic` 无输出。
- `grep -rn "sa.inspect" backend/alembic/versions/` 无输出，证明无"猜列存在"分支。
- `SELECT indexname FROM pg_indexes WHERE tablename='ai_conversation'` 含 `update_time` 索引；`tablename='ai_message'` 含 `conversation_id` 索引。
- 删除 `alembic_version` 中的行后重启服务，进程以非 0 退出，日志含"迁移版本不匹配"。
- `pytest backend/tests/test_migrations.py` 通过，且该文件含一条"迁移后表结构 == ORM 元数据"断言（`alembic.autogenerate.compare_metadata` 返回空列表）。
- `bash backend/scripts/check_migrations.sh` 返回 0。
- CI 中 `alembic upgrade head` 与 `alembic downgrade -1` 均返回 0。
