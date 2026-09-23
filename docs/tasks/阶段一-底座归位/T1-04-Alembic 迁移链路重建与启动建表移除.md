# T1-04 Alembic 迁移链路重建与启动建表移除

## 这一项要做什么

现有 `backend/alembic/versions/0001_initial.py` 的 `upgrade()` 只有一行 `Base.metadata.create_all(bind=bind)`，`0002_broadcast.py` 靠 `sa.inspect` 判断列是否存在再 `add_column`。这不是迁移，是"把 ORM 元数据当表结构真相"的障眼法：它让首次迁移必然成功，也让后续任何字段变更都不可追溯，最终表现为"开发机能跑、生产缺字段"。

本项要把迁移链路换成真正的 DDL 序列：用 `--autogenerate` 生成基线并人工核对，删掉启动时自动建表路径，改启动时校验 `alembic_version` 与代码期望一致，不一致则拒绝启动。这是产品说明书 9.2 第 7 条之外、技术方案 12.8 明确点名的硬要求。

## 前置依赖

- 依赖 T1-02 已提供：`CREATE_EXTENSION` 之后可用的异步引擎、`DATABASE_URL` 与 `CHECKPOINT_DB_URL`、`psycopg` 驱动。
- 依赖 T1-03 已提供：`vector` / `timescaledb` 扩展、`knowledge` 与 `checkpoint` schema、`env_reading` 超表结构。
- 外部环境：一个干净的空库（`alembic upgrade head` 必须在空库上一次成功）。
- 需要人工确认：现有 10 张表（`user`、`farmer_profile`、`crop`、`field`、`device`、`location`、`broadcast_log`、`ai_conversation`、`ai_message`、`memory_summary`、`user_profile_tag`）是否全部保留；技术方案 12.1 的表清单要新增流程配置、知识、处方卡、回访、快照、触达、闸门、发件箱等表，这些表由第二步任务建，**本项只建现状表的基线迁移**，新表各自在自己的任务里追加迁移。

## 参考章节

- 技术方案 第12章 12.8 迁移策略（重建方式三条、MySQL→PG 差异见 T1-14）
- 技术方案 第16章 16.2 第一步：底座归位（技术动作 2：迁移链路重建）
- 技术方案 第15章 15.4 测试策略（CI 门禁：`upgrade head` 与 `downgrade -1`）
- 技术方案 第2章 2.1 现有技术栈盘点（"迁移链路需重建"）
- 产品说明书 9.1 数据层约束、9.4 "迁移改用真实迁移链路"

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 删除 | `backend/alembic/versions/0001_initial.py` | 移除 `create_all` 伪迁移 |
| 删除 | `backend/alembic/versions/0002_broadcast.py` | 内容并入基线，`inspect` 分支去掉 |
| 新增 | `backend/alembic/versions/0001_baseline.py` | 全量 `op.create_table` 基线，含索引与约束 |
| 新增 | `backend/alembic/versions/0002_checkpoint_tables.py` | 检查点表独立成一支（可选，见实现要点六） |
| 修改 | `backend/alembic/env.py` | 支持 `compare_type` / `compare_server_default`，运行时读模式 |
| 修改 | `backend/alembic.ini` | 设 `script_location`、`version_table`、`file_template` |
| 新增 | `backend/app/core/startup_check.py` | 启动时校验 `alembic_version` 与 `EXPECTED_REVISION` |
| 修改 | `backend/app/main.py` | lifespan 调用 `verify_schema_revision()`，失败即退出 |
| 修改 | `backend/tests/conftest.py` | 用迁移建库替代 `Base.metadata.create_all` |
| 新增 | `backend/tests/test_migrations.py` | 断言 `upgrade head` / `downgrade -1` 可执行与幂等 |
| 新增 | `backend/scripts/check_migrations.sh` | CI 用：空库 upgrade + 回滚一步再前滚 |

## 实现要点

### 一、基线迁移必须"人工核对"而非直接提交

技术方案 12.8 第一条要求"以现有 `models/` 为基准，用 `alembic revision --autogenerate` 生成基线迁移，**人工核对**每个字段的类型、索引、约束"。执行步骤如下：

```powershell
# 1. 干净空库
createdb huanong_baseline
# 2. 让 autogenerate 看到"空库 vs 全量模型"的差异
alembic revision --autogenerate -m "baseline" --rev-id 0001
# 3. 逐行核对生成的 0001_baseline.py
# 4. 核对通过后再升级
alembic upgrade head
```

autogenerate 会漏掉四类东西，必须手工补：

第一，`server_default` 里的 `func.now()` 在 PG 下会生成 `DEFAULT now()`，但要核对是否带时区（依赖 T1-02 的 `DateTime(timezone=True)`）。第二，唯一约束与复合索引经常被漏（如 `user.openid` 的 `unique=True`、`device.device_sn` 的 `unique=True`）。第三，外键：现状为无外键设计，产品说明书 9.4 要求"关键关联补充外键约束"，但技术方案 12.7 把外键补齐列为高优先级项，本项**只在基线的既有表之间补外键，新增表的外键各自任务负责**。第四，`String(32)` 与 `String(36)` 的主键长度：现状是 `String(32)`，全局约定要求 `String(36)`，本项统一改为 36，并在迁移里显式写长度。

### 二、基线迁移的结构

```python
"""基线：按现有 ORM 模型建立全部业务表

Revision ID: 0001
Revises:
Create Date: 2026-09-23
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
        comment="用户表",
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


def downgrade() -> None:
    for table in (
        "user_profile_tag", "memory_summary", "ai_message", "ai_conversation",
        "broadcast_log", "location", "device", "field", "crop", "farmer_profile", "user",
    ):
        op.drop_table(table)
```

`downgrade` 按依赖逆序 `drop_table`，顺序必须人工核对，写错会在有外键时失败。`ai_message.id` 从 `Integer` 改 `BigInteger` 是有意的：消息表是增长最快的表，`Integer` 上限 21 亿在本产品规模下 5 年内可能触及。

### 三、删除启动建表路径

现有代码里 `Base.metadata.create_all` 只出现在三处：`0001_initial.py`（本项删除）、`tests/conftest.py`（本项改为迁移建库）、以及"若无 `0001` 则不会触发"。要新增一条 CI 检查，防止它被重新引入：

```bash
# scripts/check_migrations.sh 片段
if grep -rn "metadata.create_all" backend/app backend/alembic; then
  echo "禁止在应用与迁移中使用 metadata.create_all，请改为显式迁移"
  exit 1
fi
```

`tests/conftest.py` 的测试库改为调用迁移：

```python
@pytest.fixture(scope="session")
def migrated_db():
    """用迁移建测试库，保证迁移链路本身也被测试覆盖。"""
    from alembic.config import Config
    from alembic import command
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL_SYNC"])
    command.upgrade(cfg, "head")
    yield
```

测试库从 SQLite 内存改为"本地 PG 的临时库"，因为 `create_hypertable` 与 `vector` 类型在 SQLite 上不存在。这是对技术方案的一处推断，方案未明确测试库形态；若 CI 无 PG，则退化为 session 级 Docker PG。

### 四、启动时校验迁移版本

技术方案 12.8 第二条的落地：

```python
# app/core/startup_check.py
from pathlib import Path

EXPECTED_REVISION = "0001"   # 每次新增迁移后由发布流程更新
_REVISION_FILE = Path(__file__).resolve().parents[2] / "alembic" / "HEAD"


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

`EXPECTED_REVISION` 与 `alembic/HEAD` 双写是刻意的：前者是代码里的断言值，后者由 `alembic heads` 生成、作为发布产物的校验文件。两者都由发布流程刷新，避免"迁移加了但忘了更新期望值"。

### 五、env.py 改造

```python
def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,               # 类型变更能被 autogenerate 检测
            compare_server_default=True,     # 默认值变更能被检测
            include_schemas=True,            # 覆盖 knowledge / checkpoint schema
            version_table_schema="public",
        )
        with context.begin_transaction():
            context.run_migrations()
```

`include_schemas=True` 是必需的，否则 `knowledge_item` 在 `knowledge` schema 下不会被纳入 autogenerate 比对。`version_table_schema="public"` 保证 `alembic_version` 固定在一个 schema，不随 `search_path` 漂移。

### 六、检查点表是否纳入迁移

LangGraph 的 `AsyncPostgresSaver.setup()` 会建自己的检查点表。它有两条路可走：

第一条，让 `setup()` 自己建，迁移不涉及。缺点是检查点表结构不受迁移版本管理，"检查点表结构与代码不匹配"无法在启动检查里发现。

第二条，把检查点表纳入迁移，`setup()` 只在表缺失时兜底。好处是全部表结构可追溯。

本项采用第一条的简化版：检查点表由 `setup()` 管理，但把 `checkpoint` schema 的创建放进 `0001_baseline.py`（保证 schema 先存在），并在启动检查里额外断言 `checkpoint` schema 下至少有 4 张表。这是对技术方案的一处推断，方案 7.5 只说"建议独立 schema `checkpoint`"，未说明是否纳入迁移管理。

### 七、与验收项的对应

- 直接对应产品说明书 9.2 第 7 条（历史列表性能）：基线把 `ai_conversation.update_time` 与 `ai_message.conversation_id` 的索引显式建出，T1-11 的批量查询才能生效。
- 直接对应产品说明书 9.2 第 8 条（会话回滚）：迁移建库后，回滚用例能在真实 PG 上跑，SQLite 不支持 `SAVEPOINT` 语义差异被消除。
- 对应技术方案 12.8 全部三条要求。

## 验收标准

- 空库上 `alembic upgrade head` 返回 0，且 `SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('user','farmer_profile','crop','field','device','location','broadcast_log','ai_conversation','ai_message','memory_summary','user_profile_tag')` 返回 11。
- `alembic downgrade -1` 返回 0；再次 `alembic upgrade head` 返回 0；两次 `upgrade head` 与 `downgrade -1` 交替执行 3 轮无错误。
- `SELECT version_num FROM alembic_version` 在 `upgrade head` 后等于 `alembic heads` 的输出。
- `grep -rn "metadata.create_all\|metadata.drop_all" backend/app backend/alembic` 无输出。
- `grep -rn "sa.inspect" backend/alembic/versions/` 无输出（证明没有"猜列存在"的分支）。
- `SELECT indexname FROM pg_indexes WHERE tablename='ai_conversation'` 包含 `update_time` 上的索引。
- 删除数据库中的 `alembic_version` 行后重启服务，进程以非 0 退出并打印包含"迁移版本不匹配"的日志。
- `pytest backend/tests/test_migrations.py` 全部通过，且该文件包含至少一个"迁移后表结构 == ORM 元数据"的比对断言（用 `alembic.autogenerate.compare_metadata` 返回空列表）。
- `bash backend/scripts/check_migrations.sh` 返回 0。
- CI 中 `alembic upgrade head` 与 `alembic downgrade -1` 两条命令均返回 0（对应全局约定第一节的写法要求）。
