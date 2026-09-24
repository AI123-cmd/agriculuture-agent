# 迁移校验报告（staging）

- 生成时间：2026-09-24 09:05:16
- 源库时区：`Asia/Shanghai`
- 总体结论：**全部通过 ✅**

## 1. 行数一致　✅

11 张表逐表比对，全部一致或差额已声明并附理由

| 表 | 源行数 | 目标行数 | 差额 | 结果 | 理由 |
| --- | ---: | ---: | ---: | --- | --- |
| `user` | 6 | 6 | 0 | ✅ | — |
| `farmer_profile` | 1 | 1 | 0 | ✅ | — |
| `crop` | 1 | 1 | 0 | ✅ | — |
| `field` | 1 | 1 | 0 | ✅ | — |
| `device` | 2 | 1 | 1 | ✅ 差额已声明 | 未绑定设备（user_id 为空）不迁移：PG 侧归属列已收紧为 NOT NULL，无归属的设备无法落库 |
| `location` | 1 | 1 | 0 | ✅ | — |
| `user_profile_tag` | 1 | 1 | 0 | ✅ | — |
| `memory_summary` | 1 | 1 | 0 | ✅ | — |
| `ai_conversation` | 1 | 1 | 0 | ✅ | — |
| `ai_message` | 3 | 3 | 0 | ✅ | — |
| `broadcast_log` | 2 | 2 | 0 | ✅ | — |

## 2. 归属非空　✅

9 张带归属列的表全部非空

| 表 | user_id 为空的行数 | 结果 |
| --- | ---: | --- |
| `farmer_profile` | 0 | ✅ |
| `crop` | 0 | ✅ |
| `field` | 0 | ✅ |
| `device` | 0 | ✅ |
| `location` | 0 | ✅ |
| `user_profile_tag` | 0 | ✅ |
| `memory_summary` | 0 | ✅ |
| `ai_conversation` | 0 | ✅ |
| `broadcast_log` | 0 | ✅ |

## 3. 外键完整　✅

全部外键关系无孤儿行

| 外键 | 孤儿行数 | 结果 |
| --- | ---: | --- |
| `farmer_profile.user_id` → `user.id` | 0 | ✅ |
| `ai_message.conversation_id` → `ai_conversation.id` | 0 | ✅ |

## 4. 时区正确　✅

示例值 `2026-08-30 04:00:00`，差值 0.000 秒

| 项 | 值 |
| --- | --- |
| 源库记录 | `user.id=user_probe_anchor_0830`，`create_time=2026-08-30 12:00:00`（Asia/Shanghai 本地时间） |
| 期望（源时间按 Asia/Shanghai 转 UTC） | `2026-08-30 04:00:00` |
| 实际（PG `create_time AT TIME ZONE 'UTC'`） | `2026-08-30 04:00:00` |
| 差值 | 0.000 秒（容差 ±1 秒） |

## 5. 抽样字段　✅

抽样 100 行/表，逐字段比对全部一致

| `user` | 6 | 6 | ✅ |
| `farmer_profile` | 1 | 1 | ✅ |
| `crop` | 1 | 1 | ✅ |
| `field` | 1 | 1 | ✅ |
| `device` | 1 | 1 | ✅ |
| `location` | 1 | 1 | ✅ |
| `user_profile_tag` | 1 | 1 | ✅ |
| `memory_summary` | 1 | 1 | ✅ |
| `ai_conversation` | 1 | 1 | ✅ |
| `ai_message` | 3 | 3 | ✅ |
| `broadcast_log` | 2 | 2 | ✅ |
