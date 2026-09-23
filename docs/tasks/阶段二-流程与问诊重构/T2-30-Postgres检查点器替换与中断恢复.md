# T2-30 Postgres 检查点器替换与中断恢复

## 这一项要做什么

把 `app/agent/checkpointer.py` 从 `AsyncSqliteSaver` 换成 `AsyncPostgresSaver`，用连接池替代单连接，检查点表建在独立 schema `checkpoint` 中。现有实现创建单个 SQLite 连接并把它绑定到当前事件循环（代码里用 `_saver_loop` 显式判断并重建），这在单进程开发下可用，在生产多 worker 部署下有三个硬问题（见下）。本项同时把 `app/agent/__init__.py` 的 `get_agent()` 里绑事件循环的重建逻辑去掉，因为连接池不绑循环。

## 前置依赖

- T1-02 PostgreSQL 接入与异步数据库引擎改造：提供 `get_settings().CHECKPOINT_DB_URL` 与 async 驱动（`psycopg`）。
- T1-04 Alembic 迁移链路重建与启动建表移除：检查点表不进业务迁移，由 `setup()` 自建；但数据库本身必须已可连、`checkpoint` schema 必须由本项或迁移预建。
- T2-25 主图组装 main：`build_graph()` 编译时注入 checkpointer，接口形态不变。
- T2-32 人工介入与断点续问：依赖本项的持久化能力实现跨请求恢复。
- 外部环境：PostgreSQL 16+ 已启动；`langgraph-checkpoint-postgres` 已装且包含 `AsyncPostgresSaver`；连接用户具备在当前库创建 schema 与表的权限（或用迁移预建后仅需增删改权限）；`psycopg_pool` 可用。

## 参考章节

- 技术方案 第七章 7.5 检查点实现（`AsyncPostgresSaver` 单例完整代码、替换 SQLite 的三条理由、独立 schema）
- 技术方案 第七章 7.3 状态机推进规则（中断与恢复的三个场景表）
- 技术方案 第七章 7.3 人工介入（interrupt 的两类用法）
- 技术方案 第十五章 15.4 测试与质量（图测试用 `astream` 跑通路径、断言节点访问顺序）
- 全局约定 七、环境与工具（PostgreSQL 16+）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 修改 | `app/agent/checkpointer.py` | 换成 `AsyncPostgresSaver` 单例，含连接池与 `setup()` |
| 修改 | `app/agent/__init__.py` | 去掉 `_agent_loop` 重建逻辑，图的单例不再绑事件循环 |
| 修改 | `app/core/config.py` | 新增 `CHECKPOINT_DB_URL` 与 `CHECKPOINT_SCHEMA` 配置项 |
| 新增 | `alembic/versions/xxxx_checkpoint_schema.py` | 预建 `checkpoint` schema（可选，见实现要点） |
| 新增 | `tests/agent/test_checkpointer.py` | 单例幂等、跨循环复用、setup 幂等、断点续读 |
| 删除 | `aiosqlite` 检查点相关依赖 | 确认无其他引用后从 `requirements.txt` 移除 |

## 实现要点

### 替换 AsyncSqliteSaver 的三条理由

| 序号 | 问题 | 具体表现 | Postgres 如何解决 |
| --- | --- | --- | --- |
| 1 | 多进程写同一 SQLite 文件存在锁竞争 | 生产多 worker 部署下，多个进程同时对同一 `.db` 文件写检查点，SQLite 的文件级写锁导致 `database is locked`，问诊中途失败且难复现 | 连接池 + 行级并发控制，多实例可安全并发写同一逻辑表 |
| 2 | 连接绑定事件循环 | 现有代码用 `_saver_loop` 判断循环变化后重建 saver，正是因为 `AsyncSqliteSaver` 持有单个 `aiosqlite` 连接，跨循环使用会抛错；测试隔离与多循环场景下反复重建、丢失状态 | 连接池按需取连接、用后归还，不绑定单一循环 |
| 3 | 文件存储无法水平扩展 | 检查点落在单机文件上，多副本部署时各副本数据不共享，断点续问可能被路由到没有该会话状态的实例 | 检查点持久在 PostgreSQL，所有实例读同一份状态 |

### 完整代码

```python
"""Checkpointer 单例：PostgreSQL 持久化检查点，支持多实例并发与中断恢复。"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import get_settings

_saver: AsyncPostgresSaver | None = None
_pool: AsyncConnectionPool | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """PostgreSQL checkpointer 单例：支持多实例并发与事务一致性。

    连接池不绑定事件循环，跨循环（测试隔离、多 worker）复用同一 saver 是安全的。
    """
    global _saver, _pool
    if _saver is None:
        settings = get_settings()
        _pool = AsyncConnectionPool(
            settings.CHECKPOINT_DB_URL,
            min_size=2,
            max_size=20,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        await _pool.open()
        _saver = AsyncPostgresSaver(_pool, schema=settings.CHECKPOINT_SCHEMA)
        await _saver.setup()          # 幂等：已存在则跳过
    return _saver


async def close_checkpointer() -> None:
    """应用关闭时释放连接池，避免连接泄漏。"""
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _saver = None
    _pool = None
```

### 连接池参数说明

| 参数 | 取值 | 理由 |
| --- | --- | --- |
| `min_size` | `2` | 保持最小常驻连接，避免每轮问诊冷启动连接开销 |
| `max_size` | `20` | 单实例并发问诊上限；按 `worker 数 × 预期并发会话数` 估算后取保守值，避免连接数打满数据库 |
| `autocommit` | `True` | 检查点写入是独立的短事务，`setup()` 内部自己管理 DDL，交给驱动自动提交最省事 |
| `row_factory` | `dict_row` | `AsyncPostgresSaver` 期望按列名取值 |
| `open` | `False` | 先构造、再 `await open()`，保证在事件循环内建连接，避免"在导入期创建连接"的隐患 |

### 独立 schema checkpoint

检查点表建在 `checkpoint` schema 中，与业务表同库但隔离。这样做的目的有三：一是业务表迁移（Alembic）与检查点表结构互不干扰，`langgraph` 升级带来的表结构变更不会污染业务迁移链；二是权限可以单独授予，检查点表只需增删改查，不涉及业务表的归属字段；三是清理策略可以按 schema 批量执行（例如检查点保留 90 天）。方案 7.5 的原文指出"与业务表隔离但同库同事务"——同库保证在需要时可将节点产出与检查点放在同一事务边界内，隔离则保证业务迁移不被检查点表结构绑架。

具体建 schema 有两种做法，本项推荐第二种：

第一种，纯依赖 `AsyncPostgresSaver.setup()`。它会在目标 schema 不存在时报错，因此需要 `CHECKPOINT_DB_URL` 的连接用户具备 `CREATE SCHEMA` 权限。简单但把 DDL 权限交给了运行时。

第二种，用 Alembic 预建 `checkpoint` schema，运行时连接用户只有该 schema 下的增删改查权限。生产更稳妥，也便于审计。若采用第二种，`setup()` 仍是必需的，它负责建表与索引，只是不再需要建 schema 的权限。

`CHECKPOINT_SCHEMA` 默认 `"checkpoint"`，测试环境可用 `"checkpoint_test"` 隔离。

### 图单例去掉循环判断

现有 `app/agent/__init__.py` 的 `get_agent()` 有 `_agent_loop` 判断，跨循环会重建整张图。换成 Postgres saver 后不应再重建——重建会带来重复编译开销，也掩盖了"状态被丢弃"的问题。改为只判断 `_agent is None`：

```python
"""LangGraph 编排层入口：暴露 build_graph / get_agent。"""
from .graph.main import build_graph

__all__ = ["build_graph", "get_agent"]

_agent = None


async def get_agent():
    """返回编译后的图单例（含 Postgres checkpointer，跨事件循环安全复用）。"""
    global _agent
    if _agent is None:
        _agent = await build_graph()
    return _agent
```

注意导入路径从 `.graph` 改为 `.graph.main`（旧 `app/agent/graph.py` 已由 T2-25 删除）。

### 清理与生命周期

`close_checkpointer()` 需在应用关闭钩子里调用（与数据库引擎的 `dispose()` 同一处），否则连接池会随进程退出被强制关闭，可能留下告警日志。测试中每个 session 结束时也应调用一次，保证下一个测试用干净的池。

### 三个恢复场景的落点

| 场景 | 恢复要求 | 本项提供的能力 |
| --- | --- | --- |
| 农户中途退出，稍后继续 | 同一 `session_id`（即 LangGraph `thread_id`）装载检查点，从 `stage` 继续 | 检查点持久在 PG，按 `thread_id` 可随时读出最新快照 |
| 模型服务超时 | 节点级重试（tenacity，指数退避，最多 2 次），失败走降级分支 | 检查点保证重试从节点边界开始，不重复执行已完成的节点 |
| 服务重启 | 检查点持久在 PG，重启后状态不丢 | 连接池在启动时重建，检查点数据仍在原库原 schema |

三种场景的具体恢复流程由 T2-32 实现，本项只负责把"状态能跨请求、跨进程、跨重启存活"这件事做到。

## 验收标准

- `alembic upgrade head` 与 `alembic downgrade -1` 均返回 0（若采用预建 schema 方案）；`psql` 查询 `information_schema.schemata` 中存在 `checkpoint`。
- 连续调用两次 `await get_checkpointer()`，断言 `first is second`（单例成立）。
- 在两个不同的事件循环里分别调用 `get_checkpointer()`，断言返回同一 saver 对象且不抛异常（跨循环复用成立，这是替换 SQLite 的核心收益）。
- `await _saver.setup()` 连续执行两次不报错（幂等）。
- 问答链路中断恢复集成测试：用同一 `thread_id` 跑一次部分链路后丢弃图实例，重新 `build_graph()` 并用同一 `thread_id` 继续，断言能从断点处的 `stage` 继续，且 `messages`、`slots` 未丢失。
- `pytest tests/agent/test_checkpointer.py` 全部通过，且测试结束后 `pg_stat_activity` 中没有本测试残留的空闲连接（`close_checkpointer()` 生效）。
- `ruff check`、`ruff format --check`、`mypy app/agent/checkpointer.py` 均返回 0。
- `grep -rn "AsyncSqliteSaver\|aiosqlite" app/` 无输出，确认旧实现已彻底下线。
