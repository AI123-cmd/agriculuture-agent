# T1-14 MySQL 历史数据迁移到 PostgreSQL

## 这一项要做什么

现有数据在 MySQL 8.0（`mysql+pymysql://root:password@localhost:3306/huanong`），改造后主库为 PostgreSQL。本项提供一次性数据迁移脚本：从 MySQL 导出 → 按 PG 类型与语义转换 → 导入 PG → 生成校验报告。技术方案 12.8 已给出建议"在开发阶段就使用 PG，避免先迁移结构再迁移数据的双重风险"；本项服务于"必须保留历史数据"的场景，同时必须在 staging 用生产数据副本演练一次并记录耗时与校验结果。

对应技术方案 12.8 末尾段的三类数据差异与产品说明书 9.4"迁移改用真实迁移链路"。

## 前置依赖

- 依赖 T1-04 已提供：真实迁移链路已建好，PG 侧表结构可通过 `alembic upgrade head` 一次建成。
- 依赖 T1-05 已提供：`OwnedMixin` 与 `String(36)` 主键约定，迁移后每张业务表的 `user_id` 非空且已建索引。
- 依赖 T1-02/T1-03 已提供：PG 实例、`vector`/`timescaledb` 扩展、`knowledge`/`checkpoint` schema。
- 外部环境：MySQL 8.0 只读账号（迁移期间源库不得写入）；可容纳全量数据的磁盘；staging 环境一份生产数据副本。
- 需要人工确认：哪些表必须迁移、哪些可丢弃。建议迁移 `user`、`farmer_profile`、`crop`、`field`、`device`、`location`、`broadcast_log`、`ai_conversation`、`ai_message`、`memory_summary`、`user_profile_tag` 共 11 张；`device` 若含未绑定设备（`user_id` 为 NULL）需先决定是否保留。

## 参考章节

- 技术方案 第12章 12.8 迁移策略（重建方式三条、三类差异、staging 演练要求）
- 技术方案 第12章 12.1 表清单总览（表与归属列对照）
- 技术方案 第16章 16.2 第一步：底座归位（风险提示：先 staging 用生产副本演练）
- 技术方案 第4章 4.3 为什么是 PostgreSQL 统一栈（末尾"代价"段）
- 产品说明书 9.4 数据模型建议调整

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `backend/scripts/migrate_mysql_to_pg.py` | 主迁移脚本：读 MySQL、转换、写 PG |
| 新增 | `backend/scripts/migrate/type_converters.py` | 三类差异的转换函数（JSON/布尔/时区） |
| 新增 | `backend/scripts/migrate/table_order.py` | 表迁移顺序与外键依赖 |
| 新增 | `backend/scripts/verify_migration.py` | 校验脚本：行数、抽样字段、外键完整性 |
| 新增 | `backend/scripts/migrate/README` 说明段 | 写进脚本文件头的运行步骤，不单独建文件 |
| 新增 | `backend/tests/test_type_converters.py` | 转换函数的纯函数单测 |
| 新增 | `backend/reports/migration-<env>-<date>.md` | 演练后生成的校验报告（产物，非代码） |

## 实现要点

### 一、为什么先建结构再迁数据，而不是双写过渡

三种可选路径：

路径一，双写过渡（新旧库同时写，逐步切换）。优点是回滚容易；缺点是本项目从 MySQL 迁到 PG 同时还要改表结构（技术方案 9.4 的调整项），双写意味着两套表结构要同时维护，改造期间的每一次表结构变更都要写两遍。这与"合并进行不构成额外浪费"的判断（4.3 代价段）相矛盾。

路径二，先迁结构再迁数据（本项采用）。一次性停机窗口：停写 → 建 PG 结构 → 迁数据 → 校验 → 切流量。优点是简单、无中间态；缺点是要求停机窗口。

路径三，CDC 增量同步（如 Debezium）。优点是几乎不停机；缺点是引入新组件，与"运维组件最少"的选型原则冲突。

本项采路径二，并要求在 staging 用生产副本演练，用实测耗时决定最终停机窗口长度（技术方案 16.2 的风险提示）。这是对"停机窗口"的一处推断——方案未给出窗口长度，也未排除 CDC；按"运维组件最少"的原则选择停机方案。

### 二、三类数据差异的处理

技术方案 12.8 明确列出三类差异。逐个给出转换规则与理由。

**差异一：JSON 字段类型。** MySQL 的 `JSON` 列通过 PyMySQL 返回的是 `str`（需要 `json.loads` 才是对象），而 PostgreSQL 的 `JSONB` 列要求传 Python 对象（`dict` / `list`）。若直接把字符串塞进 `JSONB`，PG 会把它当作一个 JSON 字符串（而非对象）存储，读出来是 `"{\"a\":1}"` 而不是 `{"a":1}`。

```python
def convert_json(value: bytes | str | None) -> object | None:
    """MySQL JSON → PG JSONB。

    PyMySQL 对 JSON 列可能返回 bytes 或 str，两者都要先解码再 loads。
    空字符串与 'null' 均返回 Python None，而不是字符串 "null"。
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    text = value.strip()
    if text in ("", "null"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败，原文前 200 字符：{text[:200]}") from exc
```

涉及列：`broadcast_log.meta`、`user_profile_tag.tags`（现状是 `JSON` 存列表）、`memory_summary.key_facts`、`location` 相关的坐标字段。转换失败的策略是抛异常并终止整表——静默丢弃 JSON 会让历史数据在 PG 里变成 NULL，事后无法发现。这是"迁移必须可核对"的要求。

**差异二：布尔值表示。** MySQL 没有布尔类型，`TINYINT(1)` 是事实标准，现状模型里大量使用 `SmallInteger`：`crop.is_main`（0/1）、`crop.status`（1 在种 / 0 已收获）、`broadcast_log.done`（0/1）、`device.status`（0 离线 / 1 在线 / 2 故障）、`user.gender`（0/1/2）。

处理规则分两类，**不能一刀切**：

第一类，语义是布尔且只有两个有效值的，迁为 PG `boolean`：`broadcast_log.done`。但要保留 API 兼容——全局约定要求"状态字段用字符串枚举值，不用整数编码"，`done` 属于执行状态，可迁为 `boolean` 并在接口层序列化为 `true/false`（现状 `broadcast_service._to_item` 已做 `bool(item.done)`，兼容）。

第二类，语义是三值及以上或状态编码的，**保持整数**，改为 PG `smallint`：`device.status`（0/1/2）、`user.gender`（0/1/2）、`crop.status`（1/0）。若把 `device.status` 迁为 `boolean`，会丢掉"故障"这一态。

```python
def convert_bool(value: int | bytes | None, *, column: str) -> bool | None:
    """MySQL TINYINT(1) → PG boolean。只接受 0/1/None，其它值报错。

    不接受 2 及以上：若某列出现 2，说明它的语义不是布尔，
    应改用 convert_smallint，而不是把它当成 True 蒙混过关。
    """
    if value is None:
        return None
    ivalue = int(value)
    if ivalue not in (0, 1):
        raise ValueError(f"{column} 出现非布尔值 {ivalue}，请改用 smallint 迁移")
    return ivalue == 1
```

这个"报错而不是宽松处理"的设计是关键：它把"某列语义被误判"这件事在迁移时暴露，而不是在业务上线后表现为"设备状态永远不是故障"。

**差异三：时间戳时区。** MySQL 的 `DATETIME` 不带时区，应用写入时用的是服务器本地时间（现状代码全部用 `datetime.now()`，即本地时区，中国为 UTC+8）。PG 侧统一 `TIMESTAMPTZ`。

```python
def convert_datetime(value: datetime | str | None, *, source_tz: str = "Asia/Shanghai") -> datetime | None:
    """MySQL naive datetime → PG aware datetime。

    MySQL DATETIME 无时区，按"源库所在时区"赋予时区后再转 UTC 存储。
    直接用 replace(tzinfo=UTC) 会把北京时间当 UTC，导致数据整体偏移 8 小时。
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        tz = ZoneInfo(source_tz)
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)
```

**这是三类差异里最容易出错的一类。** 若用 `value.replace(tzinfo=UTC)`，一条 2026-08-30 12:00 的记录会被存成 UTC 12:00，即北京时间 20:00，前端显示时间整体晚 8 小时，且"今天"的判断会错。反过来若不做处理直接写入 `TIMESTAMPTZ`，asyncpg 会按连接时区解释，结果依赖 `PGTZ` 环境变量，更加不可控。必须显式赋予 `Asia/Shanghai` 再转 UTC。

`source_tz` 作为参数而非硬编码，是为了兼容"源库在其它时区"或"源库已配置为 UTC"的情况；默认值取 `Asia/Shanghai` 并写进脚本的必填参数校验（迁移前必须确认源库时区）。

### 三、表迁移顺序与外键

T1-04 的基线迁移已给关键表补了外键。因此导入顺序必须满足外键依赖：

```python
TABLE_ORDER = (
    "user",
    "farmer_profile",
    "crop",
    "field",
    "device",
    "location",
    "user_profile_tag",
    "memory_summary",
    "ai_conversation",
    "ai_message",
    "broadcast_log",
)
```

导入前禁用外键检查不可取（PG 无此开关），正确做法是 `SET session_replication_role = replica;` 临时跳过触发器与外键。但本项不采用，因为跳过外键会让"源数据本身违反外键"的问题被掩盖。本项按拓扑顺序导入，并把"外键违反"作为校验项之一（见下节）。

### 四、迁移脚本骨架

```python
"""MySQL → PostgreSQL 一次性数据迁移。

用法（务必先备份源库）：
    python scripts/migrate_mysql_to_pg.py \
        --mysql-url "mysql+pymysql://ro:***@mysql:3306/huanong" \
        --pg-url    "postgresql+asyncpg://huanong:***@pg:5432/huanong" \
        --batch-size 1000 --dry-run

步骤：连接校验 → 逐表迁移（按 TABLE_ORDER）→ 逐表行数校验 → 输出报告。
"""
import argparse

async def migrate_table(src, dst_conn, table: str, batch_size: int, dry_run: bool) -> TableReport:
    """单表迁移：流式读取 + 批量插入 + 计数校验。"""
    ...
```

三个实现要求：

**流式读、批量写。** `ai_message` 是最大表，必须用 `server_side_cursor`（PyMySQL 的 `SSCursor`）流式读，按 `batch_size=1000` 用 `executemany` 或 PG 的 `COPY` 写入。整表读进内存会在大表上 OOM。

**`--dry-run` 必做。** 只做转换不写入，输出"将迁移 N 行、发现 M 处转换告警"。演练阶段先 `--dry-run` 一遍，确认转换函数没有异常，再正式跑。

**断点续跑。** 记录每张表的迁移游标（如 `ai_message` 的 `id` 最大值），中断后可从该表继续，不必整库重来。对 11 张表、百万级行数的场景，这个能力决定演练能否在有限窗口内迭代。

### 五、校验脚本

`verify_migration.py` 生成报告，四类校验：

| 校验 | 方法 | 通过阈值 |
| --- | --- | --- |
| 行数一致 | 逐表 `SELECT count(*)` 双侧比对 | 完全相等（`device` 若过滤未绑定设备则记录差额与理由） |
| 抽样字段 | 每表随机 100 行，逐字段比对转换后值 | 100% 一致 |
| 外键完整 | 迁移后所有外键关系无孤儿行 | 0 条孤儿 |
| 时区正确 | 抽一条已知时间的记录，断言 PG 中的 `TIMESTAMPTZ` 与源时间差为 8 小时（UTC 存储） | 差值在 ±1 秒内 |

时区校验的写法要具体：

```sql
-- 已知源库中 user.create_time = '2026-08-30 12:00:00'（北京时间）
SELECT create_time AT TIME ZONE 'UTC' FROM "user" WHERE id = '<已知id>';
-- 期望输出 2026-08-30 04:00:00
```

如果不等于 04:00 而是 12:00，说明时区转换漏做。这条校验是三类差异里唯一"错过就无法事后发现"的一类，必须单独断言。

`user_id` 归属校验也要加一条：迁移后 `SELECT count(*) FROM ai_conversation WHERE user_id IS NULL OR user_id = ''` 必须为 0，因为 T1-05 的 `OwnedMixin` 要求非空。源库若有缺 `user_id` 的历史数据，需要在迁移前修复或明确丢弃。

### 六、报告产物

`backend/reports/migration-<env>-<date>.md` 由脚本生成，含：

- 每张表的源行数、目标行数、耗时。
- 转换告警清单（哪些行、哪个字段、原值、处理方式）。
- 三类差异的处理统计（JSON 转换 N 个、布尔转换 M 个、时区转换 K 个）。
- 校验结果四张表（行数、抽样、外键、时区）。
- 总耗时与建议的停机窗口（按实测速率外推）。

报告是演练的产出物，用于决定是否进入正式迁移。技术方案 16.2 要求"记录耗时与数据校验结果"，本项把这句话落成一份可归档的文件。

### 七、与验收项的对应

- 对应技术方案 12.8 的三条重建方式与三类数据差异。
- 为产品说明书 9.2 第 1 条（归属）提供数据侧保证：迁移后无 `user_id` 为空的历史行，使 `OwnedMixin` 的非空约束可以加上。
- 为产品说明书 9.2 第 7 条（历史列表性能）提供数据基础：历史会话迁入后，T1-11 的批量查询才有真实规模可测。
- 对应产品说明书 9.4"迁移改用真实迁移链路"。本项不直接对应某条 9.2 验收项，但它是"历史数据可追溯"这一产品要求（4.5 问诊历史与农事台账）的前提。

## 验收标准

- `python -c "from scripts.migrate.type_converters import convert_json, convert_bool, convert_datetime"` 可导入，无副作用。
- `pytest backend/tests/test_type_converters.py` 全部通过，含以下断言：
  - `convert_json(b'{"a":1}')` 返回 `{"a": 1}`（dict 而非 str）；
  - `convert_json("")` 与 `convert_json("null")` 均返回 `None`；
  - `convert_json(b'{bad json')` 抛 `ValueError` 且消息含原文前 200 字符；
  - `convert_bool(1) is True`、`convert_bool(0) is False`、`convert_bool(None) is None`；
  - `convert_bool(2, column="device.status")` 抛 `ValueError` 且消息含列名；
  - `convert_datetime("2026-08-30 12:00:00")` 的返回值 `astimezone(UTC).hour == 4`（北京时间 12:00 = UTC 04:00）。
- 在 staging 生产副本上 `--dry-run` 全表跑通，无转换异常，报告列出每张表的预估行数。
- 正式迁移后，`verify_migration.py` 的四类校验全部通过，报告中"时区正确"一项的示例值为 `2026-08-30 04:00:00`。
- 迁移后 `SELECT count(*) FROM ai_conversation WHERE user_id IS NULL OR user_id = ''` 返回 0；对 11 张表逐表执行同样的非空检查均返回 0。
- 迁移后 `SELECT count(*) FROM ai_message m LEFT JOIN ai_conversation c ON c.id = m.conversation_id WHERE c.id IS NULL` 返回 0（无孤儿消息）。
- 每张表的源行数与目标行数在报告中并排展示，且差额仅出现在被显式排除的表（如未绑定设备）并附理由。
- `backend/reports/migration-staging-<date>.md` 存在，含总耗时与建议停机窗口两节。
- 断点续跑生效：杀掉迁移进程后重跑，脚本跳过已完成表且不产生重复行（对 `idempotency_key` 类唯一列用 `ON CONFLICT DO NOTHING` 或迁移前清空目标表，两者任选其一并在报告中说明）。
