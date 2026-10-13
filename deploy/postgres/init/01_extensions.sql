-- 实例级初始化（T6-10）：扩展与 schema。
--
-- **与 alembic/versions/0001_extensions.py 是同一份内容，这是刻意的双写**：
--   1. 本脚本只在该镜像**首次**初始化空数据目录时执行一次（`docker-entrypoint-initdb.d`
--      的语义）；已有数据的卷不会重跑。它的作用是让"全新一台机器的 postgres 容器"在
--      `alembic upgrade head` 之前就已具备扩展与 schema，避免迁移在裸实例上失败。
--   2. 迁移 `0001_ext` 里的同名语句用 `IF NOT EXISTS`，与本脚本重复执行是幂等的；
--      两者同时存在才不会出现"手工建的库缺 schema、靠 Compose 建的库才有"这种环境差异。
--
-- 为什么迁移里要重复一遍而不是只依赖本脚本：数据库可能不由本 Compose 创建
-- （staging 复用已有实例、或测试库由 conftest 的 `alembic upgrade head` 建出）。
-- 迁移保证"任何路径建出的库都具备这两项"，本脚本只是让首次启动少一次等待。

-- pgvector：`Vector` 列类型依赖它（知识条目向量检索）。
CREATE EXTENSION IF NOT EXISTS vector;

-- TimescaleDB：`env_reading` 超表依赖它（CASCADE 连带 timescaledb 的内部依赖）。
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 两个 schema：知识库条目落在 knowledge，LangGraph 检查点落在 checkpoint。
-- 缺失会让 search_path 被静默忽略、表落到 public，排查成本极高（0001_ext 的说明）。
CREATE SCHEMA IF NOT EXISTS knowledge;
CREATE SCHEMA IF NOT EXISTS checkpoint;
