# MySQL → PostgreSQL 迁移报告（staging）

- 生成时间：2026-09-24 09:05:16
- 执行模式：**正式迁移**
- 源库：MySQL 8.0.46 / `huanong_staging`
- 源库时区：`@@system_time_zone=?й???׼ʱ?`、`@@time_zone=SYSTEM`，实测偏移 **UTC+8**；本次按 `--source-tz Asia/Shanghai` 转换　✅ 与源库实测偏移一致
- 目标库：PostgreSQL 17.11
- 批大小：1000；断点状态文件：`F:\wx\agriculuture-agent\backend\reports\.migration-state-staging.json`

## 一、逐表结果

| 表 | 源行数 | 目标行数 | 差额 | 本次迁移 | 耗时(s) | 说明 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `user` | 6 | 6 | 0 | 6 | 0.00 | — |
| `farmer_profile` | 1 | 1 | 0 | 1 | 0.02 | — |
| `crop` | 1 | 1 | 0 | 1 | 0.00 | — |
| `field` | 1 | 1 | 0 | 1 | 0.00 | — |
| `device` | 2 | 1 | 1 | 1 | 0.00 | 未绑定设备（user_id 为空）不迁移：PG 侧归属列已收紧为 NOT NULL，无归属的设备无法落库（排除 1 行） |
| `location` | 1 | 1 | 0 | 1 | 0.00 | — |
| `user_profile_tag` | 1 | 1 | 0 | 1 | 0.00 | — |
| `memory_summary` | 1 | 1 | 0 | 1 | 0.00 | — |
| `ai_conversation` | 1 | 1 | 0 | 1 | 0.00 | — |
| `ai_message` | 3 | 3 | 0 | 3 | 0.00 | — |
| `broadcast_log` | 2 | 2 | 0 | 2 | 0.00 | — |
| **合计** | **20** | **19** | **1** | **19** | **0.14** |  |

差额仅出现在被显式过滤的表（上表「说明」列已附理由与排除行数）；其余表源行数与目标行数必须完全相等。

## 二、三类差异的处理统计

| 差异 | 处理函数 | 命中值数 |
| --- | --- | ---: |
| JSON 字段 | `convert_json`（MySQL JSON 字符串 → Python 对象 → `JSONB`） | 5 |
| 布尔值 | `convert_bool`（`TINYINT(1)` 值域校验，非 0/1 即报错终止） | 2 |
| 时间戳 | `convert_datetime`（源库时区 Asia/Shanghai → UTC） | 41 |

`broadcast_log.done` 说明：源库为 `smallint(0/1)`，PG 基线迁移 `0001_baseline` 同样建成 `SmallInteger`。本次对它执行与 `convert_bool` 同等的**值域校验**（出现 0/1 以外的值即报错终止），但落库形态维持整数——改为 `boolean` 需要新增一条改列迁移，超出 T1-14「改动范围」表所列文件。接口层 `broadcast_service._to_item` 已做 `bool(item.done)`，对外表现不变。

## 三、转换告警

无。所有值均按规则转换，无静默降级为 NULL 的字段。

## 四、断点续跑说明

- 状态文件：`F:\wx\agriculuture-agent\backend\reports\.migration-state-staging.json`（记录已完成的表与每张表的最后一行主键）。
- 幂等策略：写入统一使用 `ON CONFLICT (<主键>) DO NOTHING`。因此重复执行不会产生重复行，游标只用于减少重复读取，正确性由约束保证。
- 本次已完成表：`user`、`farmer_profile`、`crop`、`field`、`device`、`location`、`user_profile_tag`、`memory_summary`、`ai_conversation`、`ai_message`、`broadcast_log`。

## 五、耗时与建议停机窗口

- 实测总耗时：**0.14 秒**（0.00 分钟），迁移 20 行，速率 142 行/秒。
- 建议停机窗口 = 实测耗时 × 3（安全系数，覆盖生产数据量更大与索引维护开销） + 15 分钟（`alembic upgrade head` 建结构、四类校验、切流量与回滚预留）。
- **本环境建议窗口：15.0 分钟**。
- 生产环境应按本报告速率用生产实际行数重新外推，不要直接照搬本值。
