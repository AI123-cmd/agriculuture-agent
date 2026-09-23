# T6-06 OpenTelemetry 链路追踪接入

## 这一项要做什么

为编排链路接入 OpenTelemetry，落地 14.4 定义的 trace 结构：一次问诊等于一个 trace，每个智能体等于一个 span，每次模型调用等于一个 generation。多智能体链路变长后错误归因困难（17.1 风险表列出的一项），每个节点独立 span 是唯一能把"线上问题定位耗时"压下来的手段。本项只做追踪与字段写入，不含脱敏（T6-08 负责）与上报后端（T6-07 负责）。

## 前置依赖

- 依赖 T2-25 已提供：`build_main_graph()` 与七条子图，节点可被包裹；依赖 T2-24：条件边函数（本项为其加子 span）。
- 依赖 T2-08：`ConsultationState` 含 `consultation_id`、`process_id`、`round`、`symptoms`、`gate` 等字段，是 span 属性的来源。
- 依赖 T2-45、T2-46：闸门子图与 `GateResult`，`gate` 判定结果与命中规则名要写入 span。
- 依赖 T2-55、T2-56：模型网关统一入口，generation span 在网关内产生；依赖 T2-59：token 计量已提供 token 数与成本。
- 依赖 T2-49、T2-50：知识检索已提供引用的知识条目 ID 与 `region_scope`（来源类型）。
- 依赖 T1-08：`Settings` 环境隔离，`ENV=production` 时的追踪行为由本项配置。
- 外部条件：需新增依赖 `opentelemetry-api`、`opentelemetry-sdk`、`opentelemetry-exporter-otlp`；无外部 collector 时使用控制台导出器（本地开发）或 null 导出器（CI），不得因 collector 不可用而使问诊失败。
- 现有起点：`backend/app/` 下无 `observability/` 目录，本项新建；`backend/requirements.txt` 与 `backend/pyproject.toml` 需同步加依赖。

## 参考章节

- 技术方案 第14章 14.4 可观测性实现（技术栈："OpenTelemetry（链路追踪）+ Langfuse（LLM 专用观测，私有化部署）+ structlog（结构化日志）"；Trace 结构设计："一次问诊 = 一个 trace，每个智能体 = 一个 span，每次模型调用 = 一个 generation"；`traced_node` 代码骨架；必须写入 trace 的字段清单；"敏感信息处理：trace 中不写完整农户手机号……用 `ObservabilityMasker` 统一脱敏"）
- 技术方案 第17章 17.1 技术风险（"多智能体链路变长后错误归因困难 → 每个节点独立 span + `gate_audit` 记录完整判定依据（14.4）"）
- 技术方案 第7章 7.2 代码结构（`agent/graph/` 各文件职责，节点包裹点）
- 技术方案 第10章 10.4 成本控制（按流程、按智能体、按任务的 token 成本明细）
- 全局约定 第二节 目标目录结构（`app/observability/` 四文件）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/observability/__init__.py` | 可观测性子包 |
| 新增 | `app/observability/tracing.py` | `traced_node`、`traced_generation`、tracer 初始化与导出器选择 |
| 新增 | `app/observability/attributes.py` | span 属性键常量（统一键名，避免各处拼字符串） |
| 修改 | `app/agent/graph/main.py` | 节点经 `traced_node` 包裹（T2-25 预留接入点） |
| 修改 | `app/agent/graph/factories.py` | 通用节点工厂统一套追踪，避免逐个节点手写 |
| 修改 | `app/agent/graph/gate_graph.py` | 闸门子图节点加 span，写入判定结果与命中规则 |
| 修改 | `app/llm/gateway.py` | 每次模型调用开 generation span（T2-55 接入点） |
| 修改 | `app/core/config.py` | 新增 `OTEL_ENABLED`、`OTEL_EXPORTER`、`OTEL_ENDPOINT`、`OTEL_SERVICE_NAME` |
| 修改 | `backend/.env.example` | 补上述环境变量与注释 |
| 新增 | `backend/tests/observability/__init__.py` | 测试包 |
| 新增 | `backend/tests/observability/test_tracing.py` | 用内存导出器断言 span 名称、父子关系与属性 |
| 新增 | `backend/tests/observability/test_trace_attributes.py` | 断言 14.4 要求的字段全部写入 |

## 实现要点

### 一、traced_node 原样保留（技术方案 14.4 原文）

```python
# observability/tracing.py
async def traced_node(name: str, fn, state: ConsultationState) -> ConsultationState:
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("consultation.id", state.consultation_id)
        span.set_attribute("process.id", state.process_id.value if state.process_id else "unknown")
        span.set_attribute("node.round", state.round)
        try:
            result = await fn(state)
            span.set_attribute("node.ok", True)
            return result
        except Exception as exc:
            span.set_attribute("node.ok", False)
            span.record_exception(exc)
            raise
```

三处要写进注释：`state.process_id` 在 `intake` 之后、`triage` 之前为 `None`，因此属性值用 `"unknown"` 兜底而不是 `str(None)`，让"确实未知"与"字符串 None"可区分；`span.record_exception(exc)` 与 `raise` 必须成对，span 记录异常不等于吞掉异常（`gate/` 与守卫类节点 fail-closed，异常必须继续上抛）；`node.ok` 用布尔而不是异常字符串，便于后端按 `node.ok = false` 聚合失败率（14.3 链路级指标"各节点失败率"）。

### 二、必须写入 trace 的字段

14.4 列了五组字段，逐组落到属性和产生位置：

| 字段组 | 属性键 | 产生位置 |
| --- | --- | --- |
| `consultation_id` / `process_id` / `round` | `consultation.id`、`process.id`、`node.round` | `traced_node` 每节点 |
| 命中的槽位与问出的问题 ID | `interview.slots_hit`、`interview.question_ids` | 问诊节点（`interview`） |
| 引用的知识条目 ID 与来源类型 | `evidence.ref_ids`、`evidence.source_types` | 证据核查节点（`verify`）与检索层 |
| 闸门判定结果与命中的规则名 | `gate.decision`、`gate.rules_hit` | 闸门子图节点 |
| 模型名、供应商、token 数、耗时、是否降级 | `llm.model`、`llm.provider`、`llm.tokens`、`llm.latency_ms`、`llm.degraded` | generation span（模型网关内） |

`evidence.ref_ids` 与 `facts_provenance` 对应（14.4 括注）；属性值用列表时统一转成逗号分隔字符串或 OTel 支持的序列类型，且长度截断（单次问诊引用可能上百条，全量写入会让 span 过大）。截断阈值放 `attributes.py` 常量，并同时写入 `evidence.ref_ids_count` 保留总数，否则截断后会低估引用量。

### 三、trace 结构设计

三层嵌套关系由 OTel 的当前 span 栈自然形成，无需手工传 parent：

- **一次问诊 = 一个 trace**：在问诊入口（T2-33 的接口或 `build_main_graph` 的调用处）开一个根 span（如 `consultation`），并把 `thread_id`（`consultation_id`）作为属性，使 trace 可按会话检索。
- **每个智能体 = 一个 span**：`traced_node` 在节点执行期处于"当前 span"位置，因此模型调用与检索调用自动成为它的子 span。
- **每次模型调用 = 一个 generation**：在 `llm/gateway.py` 内开 span，名称固定 `generation`，用 `gen_ai.*` 约定的属性键记录模型与 token（与 Langfuse 的语义对齐，见 T6-07）。

必须有一处显式约束：**不要用 asyncio.create_task 把节点执行扔到后台**，否则上下文不会传播，子 span 会挂到错误的父节点上。并行会诊 panel（T2-20）是唯一的并行节点，用例要专门断言其子 span 的父 span 是 `panel` 而非主图。

### 四、导出器选择与降级

```python
def configure_tracing(settings: Settings) -> None:
    """导出器三档：otlp（生产，指向自建 collector）、console（本地）、none（CI）。"""
    if not settings.OTEL_ENABLED or settings.OTEL_EXPORTER == "none":
        return                                        # 不注册 provider，开销为零
    exporter = (OTLPExporter(endpoint=settings.OTEL_ENDPOINT)
                if settings.OTEL_EXPORTER == "otlp" else ConsoleSpanExporter())
    provider = TracerProvider(resource=Resource.create({"service.name": settings.OTEL_SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
```

`OTEL_ENABLED=false` 时完全不注册 provider，`start_as_current_span` 退化为无操作 span，性能开销可忽略，因此生产可按需关闭。批量导出器（`BatchSpanProcessor`）而非简单导出器是硬要求：简单导出器在每次 span 结束时同步阻塞，会把"农闲期也能用的服务"变成"collector 抖动即问诊变慢"。导出失败不得影响问诊，`BatchSpanProcessor` 默认丢弃并记账，本项不额外抛错。

`ENV=production` 时若 `OTEL_ENABLED=true` 而 `OTEL_ENDPOINT` 为空，启动检查应报违规（指向 T1-08 的 `collect_violations`）——否则生产会静默走控制台导出，追踪数据直接落到容器日志里，可能是隐私外泄路径（与 T6-08 的脱敏防线叠加）。

### 五、与日志、指标的分工

三项可观测能力不要互相替代：trace 回答"这一步为什么慢/为什么走这条路"（跨节点因果），日志回答"这次判定细节是什么"（T6-08），指标回答"整体趋势如何"（T6-09）。本项只在 trace 里写结构化属性，不写日志、不打点；需要 trace 与日志关联时，用 `trace_id` 与 `span_id` 作为日志字段（T6-08 实现），本项在 `tracing.py` 提供 `current_trace_context()` 取值函数供日志模块调用。

## 验收标准

- `python -m pytest backend/tests/observability/test_tracing.py` 通过：用内存导出器（`InMemorySpanExporter`）跑一次问诊后，断言 span 数量等于执行节点数加模型调用数；根 span 名为 `consultation`，子 span 名为节点名，generation span 名为 `generation`。
- 断言父子关系：`traced_node` 内抛异常时，span 的 `status` 为 `ERROR` 且属性 `node.ok` 为 `False`，同时异常继续上抛（用例用 `pytest.raises` 捕获）。
- `test_trace_attributes.py` 通过：一次完整问诊的 span 上存在 `consultation.id`、`process.id`、`node.round`、`interview.slots_hit`、`interview.question_ids`、`evidence.ref_ids`、`evidence.source_types`、`gate.decision`、`gate.rules_hit`；generation span 上存在 `llm.model`、`llm.provider`、`llm.tokens`、`llm.latency_ms`、`llm.degraded`。
- `evidence.ref_ids` 超过截断阈值时，属性被截断且 `evidence.ref_ids_count` 等于真实总数；断言二者同时存在。
- `OTEL_ENABLED=false` 时跑一次问诊，导出器中 span 数为 0，且问诊结果与开启时一致（关闭追踪不改行为）。
- `OTEL_EXPORTER=none` 与 `OTEL_EXPORTER=console` 均可正常启动；`OTEL_EXPORTER=otlp` 且 endpoint 不可达时，问诊仍成功返回（导出失败不影响业务）。
- 并行会诊（`process_id=chili` 的用药问题）触发的 `panel` 子 span，其父 span 为 `panel` 而非主图节点，证明上下文在并行分支内正确传播。
- `current_trace_context()` 在图执行期间返回非空的 `trace_id` 与 `span_id`，在图外返回 `None`。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0；`model_gateway`（`app/llm/`）未导入任何业务模块（L3 规则保持通过）。
