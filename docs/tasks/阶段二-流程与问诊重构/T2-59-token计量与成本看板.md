# T2-59 token 计量与成本看板

## 这一项要做什么

在 `app/llm/metering.py` 与 `app/models/llm_call.py` 中实现 token 计量与成本看板：每次模型调用落一条 `llm_call` 记录，含任务类型、智能体、模型、供应商、token 数、耗时、是否降级、成本估算；并在此基础上提供按流程、按智能体、按任务的成本聚合查询。它让方案 10.4 的成本控制从原则变成可观测的数据。

计量表同时承担两个职责：成本归属（哪个流程哪个环节花钱最多）与版本对比（模型升级前后同任务的消耗与质量变化）。

## 前置依赖

- 依赖 T2-55：必须已提供 `ModelGateway` 的调用编排与 `ModelResponse`
- 依赖 T1-04：必须已建立 Alembic 迁移链路，本项新增迁移 `0005_llm_call.py`
- 依赖 T2-57：必须已提供 `RoutingDecision`，计量记录档位与是否降档
- 依赖 T2-01：必须已提供 `TaskType`
- 外部环境：PostgreSQL 16+；各模型单价表由配置提供（用于成本估算）
- 与 T6-09 的关系：线上指标采集读取本表，本项提供数据与聚合入口

## 参考章节

- 技术方案 第10章 10.1 管理职责之五计量与审计（记录 token、耗时、模型、任务类型、是否降级）
- 技术方案 第10章 10.4 成本控制（计量看板：按流程、按智能体、按任务的 token 成本明细）
- 技术方案 第14章 14.3 线上指标（各节点模型 token 消耗与成本 `llm_call` 表）
- 技术方案 第15章 15.5 版本与发布（模型版本化，`llm_call` 记录模型名与版本）
- 技术方案 第12章 12.1 表清单总览（计量数据与成本相关表）
- 全局约定 第四节 数据库约定（时序表用 `BIGSERIAL` 配时间分区；业务表带 `user_id`）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/models/llm_call.py` | `LlmCall`：调用记录表 |
| 新增 | `app/llm/metering.py` | `TokenMeter`：记录与成本估算 |
| 新增 | `app/repositories/llm_call.py` | 聚合查询：按流程、按智能体、按任务 |
| 新增 | `app/services/cost_dashboard_service.py` | 成本看板的聚合入口 |
| 新增 | `alembic/versions/0005_llm_call.py` | 记录表迁移 |
| 新增 | `tests/llm/test_metering.py` | 记录字段、成本估算、聚合的用例 |
| 修改 | `app/core/config.py` | 各模型单价配置项 |

## 实现要点

### 调用记录表

```python
class LlmCall(Base):
    """一次模型调用记录。只追加，用于成本归属与版本对比。"""
    __tablename__ = "llm_call"

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)   # BIGSERIAL
    user_id = mapped_column(String(36), nullable=True, index=True)         # 匿名调用可为空
    consultation_id = mapped_column(String(36), nullable=True, index=True)
    process_id = mapped_column(String(20), nullable=True, index=True)
    agent = mapped_column(String(30), nullable=False, index=True)          # 智能体名
    task_type = mapped_column(String(30), nullable=False, index=True)
    tier = mapped_column(String(10), nullable=False)                       # 档位
    provider = mapped_column(String(30), nullable=False, index=True)
    model = mapped_column(String(60), nullable=False)
    model_version = mapped_column(String(40), nullable=True)               # 供应商返回的版本号
    prompt_tokens = mapped_column(Integer, nullable=False, default=0)
    completion_tokens = mapped_column(Integer, nullable=False, default=0)
    cost_micros = mapped_column(BigInteger, nullable=False, default=0)     # 成本，单位 1e-6 元
    latency_ms = mapped_column(Integer, nullable=False, default=0)
    is_fallback = mapped_column(Boolean, nullable=False, default=False)     # 是否走了备选
    is_degraded = mapped_column(Boolean, nullable=False, default=False)     # 是否降档
    ok = mapped_column(Boolean, nullable=False, default=True)              # 成功或最终失败
    error_code = mapped_column(String(40), nullable=True)
    input_digest = mapped_column(String(64), nullable=True)                # 输入哈希，不留原文
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)

    __table_args__ = (Index("ix_llm_call_task_created", "task_type", "created_at"),
                      Index("ix_llm_call_agent_created", "agent", "created_at"),)
```

主键用 `BIGSERIAL` 而不是 UUID：这是一张高写入量的记录表，整数主键的索引更紧凑，且它不承载归属（归属由 `user_id` 表达）。这与全局约定"时序表除外，用 `BIGSERIAL` 配合时间分区"的口径一致。

`cost_micros` 用整数存微元而不是浮点存元，避免累计误差。成本是要拿来做预算判断的，浮点累加在长期统计中会出现分位偏移。展示时除以 1e6 转为元。

`model_version` 记录供应商返回的模型版本（若返回），配合方案 15.5 的"模型升级后可对比升级前后的抽检质量"。

`input_digest` 与 T2-55 的内容审计衔接：记录输入哈希，用于回答"两次调用是否相同输入"，不存原文以保护隐私。

### 成本估算

```python
class TokenMeter:
    """token 计量与成本估算。只在调用成功或最终失败时记录一次。"""

    def __init__(self, session: AsyncSession, pricing: dict[str, ModelPricing]) -> None: ...

    def estimate_cost_micros(self, model: str, prompt_tokens: int, completion_tokens: int) -> int:
        price = self._pricing.get(model)
        if price is None:
            return 0                                  # 未配置单价的模型不计成本，但记录 token
        return (prompt_tokens * price.input_micros_per_k // 1000
                + completion_tokens * price.output_micros_per_k // 1000)

    async def record(self, task_type: TaskType, response: ModelResponse, candidate: ModelCandidate,
                     *, is_fallback: bool, is_degraded: bool, ctx: CallContext) -> None:
        ...
```

单价按"每千 token 的微元"配置（`input_micros_per_k`、`output_micros_per_k`），输入与输出分开计价，因为多数供应商的输出单价远高于输入。未配置单价的模型记录 token 但成本记 0，而不是报错：单价的更新滞后于模型上线是常态，缺单价不应阻断计量本身。

成本不参与任何安全判断，只用于统计与预算参考。它由 token 数与单价相乘得到，不涉及模型推断，因此是纯计算。

### 记录时机

计量只在两种时机写入：

| 时机 | 是否写入 | 说明 |
| --- | --- | --- |
| 调用成功 | 是 | 记录真实 token 与耗时 |
| 候选链最终失败 | 是（一条） | 记录最后一次尝试的错误码，token 记录为 0 |
| 中间候选失败后切换 | 否 | 失败尝试不计费，但计入熔断器 |

中间失败的不写入，避免"一次问诊写了三条失败记录"污染成本统计。失败的尝试确实消耗了请求，但在供应商侧通常不计费或计费不可得，因此记录为 0 更接近实际成本。若将来需要统计失败尝试的延迟成本，用链路追踪（T6-06）而非计量表。

### 看板聚合查询

```python
class LlmCallRepository:
    """计量记录仓储。所有聚合经此。"""

    async def cost_by_agent(self, since: datetime, until: datetime) -> list[AgentCostRow]:
        """按智能体聚合：调用次数、token、成本、失败率。"""
        stmt = (select(LlmCall.agent, func.count().label("calls"),
                func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens).label("tokens"),
                func.sum(LlmCall.cost_micros).label("cost"))
            .where(LlmCall.created_at.between(since, until))
            .group_by(LlmCall.agent).order_by(func.sum(LlmCall.cost_micros).desc()))
        ...

    async def cost_by_process(self, since, until) -> list[ProcessCostRow]: ...
    async def cost_by_task_type(self, since, until) -> list[TaskCostRow]: ...
    async def degrade_ratio(self, since, until) -> float:
        """降级与降档占比。区分 is_fallback 与 is_degraded 两类。"""
        ...
```

三个维度的聚合对应方案 10.4 的"按流程、按智能体、按任务的 token 成本明细"。`degrade_ratio` 单独给"降级率"与"降档率"两个比值：降档是预算控制的结果（正常），降级是服务故障的结果（异常），二者混在一起会掩盖问题。

聚合走仓储层，不使用 `db.query(Model)`，符合全局约定 L4。看板接口按日/周/月查询，`ix_llm_call_task_created` 与 `ix_llm_call_agent_created` 支撑时间范围加维度的查询。

### 与预算守卫的衔接

方案 10.4 的"单次预算：`max_tokens_per_consult` 超限即降档"需要读单次问诊的累计 token。这个累计从 `llm_call` 按 `consultation_id` 汇总得到，由预算守卫（T2-31）在每次调用前查询。因此本表要保证按 `consultation_id` 的查询效率（已有索引），且写入要在调用后立即提交，否则预算守卫读到滞后数据会导致超预算。

写入用独立事务提交（不挂在大问诊事务里），因为计量是旁路数据，不应因为业务事务回滚而丢失，也不应因为计量写失败而回滚业务。若计量写失败，记一条日志但不阻断模型调用返回。

### 隐私与脱敏

记录里不含 prompt 与 completion 原文，只有哈希与统计。这与 T2-55 的内容审计一致。`user_id` 与 `consultation_id` 用于归属与归因，属于必要字段；它们不承载内容信息。

## 验收标准

- `alembic upgrade head` 与 `alembic downgrade -1` 均返回 0，`alembic current` 显示 `0005_llm_call`
- `\d llm_call` 可见主键为 `bigint`、索引 `ix_llm_call_task_created` 与 `ix_llm_call_agent_created`
- `pytest tests/llm/test_metering.py` 全部通过，至少含九条：成功调用写入一条记录且 token 与耗时来自 `ModelResponse`、候选链最终失败只写一条记录且 `ok=False`、中间候选失败不写记录、`cost_micros` 按输入输出分别计价、未配置单价的模型 `cost_micros` 为 0 但 token 正常记录、`is_fallback` 与 `is_degraded` 分别记录、`input_digest` 为 64 位哈希且记录中不含原文、计量写失败不影响 `chat` 返回、记录含 `process_id` 与 `agent`
- 断言 `cost_by_agent` 返回按成本降序排列，且 token 为输入输出之和
- 断言 `degrade_ratio` 分别返回降级率与降档率两个值，且两者定义不同（构造一组 `is_fallback` 与 `is_degraded` 混合数据验证）
- 断言聚合查询生成的 SQL 带 `created_at BETWEEN` 且按维度 `GROUP BY`（用 `str(stmt)` 断言）
- 断言仓储类不出现 `db.query(` 调用（`scripts/check_layers.py` 的 L4 检查通过）
- 断言 `estimate_cost_micros` 对输入 1000 token、输出 1000 token、单价各 1000 微元每千 token 的模型返回 2000
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
