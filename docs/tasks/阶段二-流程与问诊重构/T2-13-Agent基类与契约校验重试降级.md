# T2-13 Agent 基类与契约校验重试降级

## 这一项要做什么

在 `app/agent/agents/base.py` 中实现智能体基类，把契约校验、失败重试、降级路径与结构化输出强制这四件横切的事收在一处。每个具体智能体只需声明它的输出契约类型、提示词构造方式、降级实现与失败行为类别，不重复写解析与重试逻辑。 这四件事之所以要统一下沉，是因为它们决定了安全链路的行为：解析失败必须触发降级而不是正则硬抽，重试必须有限次且有退避，守卫类智能体必须 fail-closed 而其余可以降级继续服务。散落在七个智能体里各写一遍，一定会有某一处写漏。

## 前置依赖

- 依赖 T2-56：必须已提供模型供应商适配器与结构化输出能力（`gateway.chat(task_type, messages, schema)`）；依赖 T2-09 至 T2-12：必须已提供全部契约类型，基类以泛型约束它们
- 依赖 T2-55：必须已提供模型网关统一入口；依赖 T2-01：必须已提供 `TaskType`
- 外部环境：`tenacity` 已写入 `requirements.txt`；`USE_MOCK_LLM=true` 时网关返回预设结构化响应；与 T2-24 的关系：基类负责节点内部逻辑，主图的条件边由 T2-24 组装

## 参考章节

- 技术方案 第5章 5.1 智能体总表（十项智能体的类型、自治程度、失败行为）；技术方案 第5章 5.2 契约设计总原则（枚举封闭、解析失败即契约违约）
- 技术方案 第7章 7.3 状态机推进规则（节点级重试 tenacity 指数退避最多 2 次）；技术方案 第10章 10.2 供应商适配（结构化输出的三层保障）
- 技术方案 第10章 10.3 降级链路（问诊模板问题、判定不确定、处方转农技站）；技术方案 第6章 6.5 独立仲裁（生成链路降级继续，仲裁链路 fail-closed）
- 全局约定 第六节（失败行为分两类）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/__init__.py` | 智能体包 |
| 新增 | `app/agent/agents/base.py` | `BaseAgent`、`AgentContext`、`AgentFailure`、`FailureMode` |
| 新增 | `app/agent/agents/exceptions.py` | `ContractViolation`、`AgentDegraded` |
| 新增 | `tests/agent/test_base_agent.py` | 重试、降级、fail-closed 的六类用例 |
| 修改 | `requirements.txt` | 确认 `tenacity>=8.2` |

## 实现要点

### 失败行为分类

```python
class FailureMode(str, Enum):
    """智能体失败行为。仅守卫类使用 FAIL_CLOSED。"""
    DEGRADE = "degrade"          # 降级但仍服务
    FAIL_CLOSED = "fail_closed"  # 不确定时选更保守的一方
```

技术方案 5.1 明确十项智能体中只有报告守卫与安全闸门是 fail-closed。基类把这一项设为类属性，并在 `_on_failure` 里按它分派两条不同路径。这样"某个智能体的失败行为"是一个可被静态读取的声明，而不是藏在实现里的分支。

### 运行上下文

```python
@dataclass(slots=True)
class AgentContext:
    """一次智能体调用的环境依赖。全部为只读视图。"""
    process_config: ProcessConfig
    config_version: int
    task_type: TaskType
    trace_id: str
    region_code: str | None = None
    budget_remaining_tokens: int | None = None
```

`AgentContext` 刻意不含数据库会话。L2 规则要求 `app/agent/` 不得直接导入 `app/models/`，智能体要能被喂假数据单测。需要持久化的智能体（如证据核查）通过注入的仓储接口访问数据，而不是拿 `AsyncSession`。

### 基类骨架

```python
OutT = TypeVar("OutT", bound=BaseModel)

class BaseAgent(Generic[OutT], ABC):
    """智能体基类：契约校验 + 重试 + 降级 + 结构化输出。"""
    name: ClassVar[str]
    output_schema: ClassVar[type[BaseModel]]
    task_type: ClassVar[TaskType]
    failure_mode: ClassVar[FailureMode] = FailureMode.DEGRADE
    max_attempts: ClassVar[int] = 2          # 首次之外的额外尝试次数，总计最多 3 次调用
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway
    # ---- 子类必须实现 ----
    @abstractmethod
    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词。结构化字段注入，不拼接自由文本。"""
    @abstractmethod
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> OutT:
        """降级路径。必须返回满足契约的对象，不得返回 None。"""
    # ---- 子类可选覆盖 ----
    async def post_validate(self, result: OutT, state: ConsultationState, ctx: AgentContext) -> OutT:
        """契约之外的业务校验，如判定契约的排除项非空检查。"""
        return result
    def validate_output(self, result: OutT, state: ConsultationState, ctx: AgentContext) -> None:
        """可覆盖的额外校验，抛 ContractViolation 触发重试。"""
        return None
    # ---- 统一入口 ----
    async def run(self, state: ConsultationState, ctx: AgentContext) -> OutT:
        try:
            result = await self._call_with_retry(state, ctx)
            result = await self.post_validate(result, state, ctx)
            log.info("agent ok", extra={"agent": self.name, "trace_id": ctx.trace_id})
            return result
        except Exception as exc:                 # noqa: BLE001
            return await self._on_failure(state, ctx, exc)
    async def _call_with_retry(self, state: ConsultationState, ctx: AgentContext) -> OutT:
        """带指数退避的重试。仅对契约违约与瞬时故障重试。"""
        attempt_state: dict[str, Any] = {"n": 0, "last_error": None}
        @retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type((ContractViolation, TransientModelError)),
            reraise=True,
            before_sleep=self._log_retry,
        )
        async def _attempt() -> OutT:
            attempt_state["n"] += 1
            messages = self.build_messages(state, ctx)
            if attempt_state["n"] > 1:
                messages = self._append_repair_hint(messages, attempt_state["last_error"])
            response = await self._gateway.chat(ctx.task_type, messages, self.output_schema)
            if response.parsed is None:
                attempt_state["last_error"] = response.error or "解析失败"
                raise ContractViolation(f"{self.name} 结构化输出解析失败")
            result = self.output_schema.model_validate(response.parsed)
            self.validate_output(result, state, ctx)
            return cast(OutT, result)
        return await _attempt()
```

重试的触发条件被严格限定为两类异常：`ContractViolation`（契约违约）与 `TransientModelError`（网关报告的瞬时故障）。业务异常（如配置缺失、越界）不重试，因为它们重试也不会成功，只会浪费 token 与延迟。

`max_attempts=2` 表示除首次外最多再试 2 次，与技术方案 7.3 的"节点级重试，指数退避，最多 2 次"一致。

### 修复提示而不是正则硬抽

```python
    def _append_repair_hint(self, messages: list[dict], last_error: str | None) -> list[dict]:
        """把上一次的校验错误反馈给模型，让它自己修。不做正则硬抽。"""
        if not last_error:
            return messages
        return [
            *messages,
            {"role": "assistant", "content": "（上一次输出未通过校验）"},
            {"role": "user", "content": f"你的上一次输出不符合要求：{last_error}。请仅输出符合 JSON Schema 的修正结果。"},
        ]
```

技术方案 10.2 明确"为什么不做解析失败就用正则硬抽"：农业场景下错误的字段值比没有字段值更危险。契约违约应当触发降级，而不是让不完整的数据流入下游。因此这里的唯一补救手段是让模型重出，重出失败就走降级。

### 降级与 fail-closed 的分派

```python
    async def _on_failure(self, state: ConsultationState, ctx: AgentContext, exc: Exception) -> OutT:
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("agent degraded", extra={"agent": self.name, "failure_mode": self.failure_mode.value, "reason": reason})
        metrics.incr("agent_degraded_total", tags={"agent": self.name, "mode": self.failure_mode.value})
        if self.failure_mode is FailureMode.FAIL_CLOSED:
            return await self.fail_closed(state, ctx, reason)
        return await self.degrade(state, ctx, reason)
    async def fail_closed(self, state: ConsultationState, ctx: AgentContext, reason: str) -> OutT:
        """守卫类失败路径。默认实现为子类未覆盖时抛异常，强制显式实现。"""
        raise NotImplementedError(f"{self.name} 声明 fail_closed 但未实现 fail_closed 路径")
```

`fail_closed` 的默认实现是抛异常而不是给一个"保守值"。原因是"保守"对于不同守卫含义不同：报告守卫的保守是"报"，安全闸门的保守是"拦"，基类无法给出通用的保守值。强制子类实现，让每个守卫显式声明自己的保守方向。

### 降级的统一约定

技术方案 10.3 的降级链路给出各环节的降级出口：

| 智能体 | 降级出口 |
| --- | --- |
| 分诊 | 回退 general 流程，`fallback_reason` 填"模型不可用" |
| 现场采集 | `data_available=False` 并给出 `unavailable_reason` |
| 问诊 | 模板问题（`why` 已在模板中写好） |
| 证据核查 | 返回空集并标注未命中 |
| 判定 | `confidence_level="uncertain"` + 求助引导 |
| 处方 | `requires_tech_station=True`，`pesticide_items=[]` |
| 并行会诊 | 转单链判定 |
| 报告守卫 | `triggered=True`（有疑即报，fail-closed） |

这张表要写进基类模块的 docstring，作为各子类实现 `degrade` 的依据。其中报告守卫一行是唯一的 fail-closed 特例。

### 结构化输出的强制

```python
    def _schema_for_prompt(self) -> str:
        """把契约的 JSON Schema 注入系统提示。与网关的原生结构化输出形成双保险。"""
        return json.dumps(self.output_schema.model_json_schema(), ensure_ascii=False)
```

结构化输出有三层保障（技术方案 10.2）：优先用供应商原生结构化输出能力；退化到提示词约束加解析，失败时用 pydantic 的错误信息构造修复提示重试一次；仍失败走降级。基类同时做前两层：把 Schema 注入提示词是第二层，把 Schema 传给网关是第一层，哪一层先成功都可以。

### 失败计数与预算

每次 `run` 结束时向指标上报三类数据：是否降级、尝试次数、耗时。尝试次数超过 1 的比例若持续升高，说明该智能体的提示词或 Schema 有问题，这比看总体失败率更灵敏。若 `ctx.budget_remaining_tokens` 低于阈值，`max_attempts` 降为 1，避免在预算紧张时反复重试。

## 验收标准

- `pytest tests/agent/test_base_agent.py` 全部通过，含六类用例：首次成功不重试、契约违约重试两次后降级、业务异常不重试直接降级、`FAIL_CLOSED` 子类走 `fail_closed` 路径、`fail_closed` 未实现时抛 `NotImplementedError`、预算不足时 `max_attempts` 降为 1
- 用 `respx` 或网关 mock 断言：单次 `run` 最多发起 3 次模型调用（首次 + 2 次重试）
- 断言重试间隔为指数退避（第二次等待约 0.5 秒，第三次约 1 秒，上限 4 秒）
- 断言降级时返回对象类型与 `output_schema` 一致，且 `degrade` 的返回值被 `post_validate` 再次校验
- 断言 `_append_repair_hint` 在第二次尝试时向 messages 追加两条消息，第一次调用时不追加
- 断言 `metrics.incr` 被调用一次且标签含 `agent` 与 `mode`
- `USE_MOCK_LLM=true` 下运行测试，无任何真实网络请求（用 `respx` 拦截全部请求并断言零请求）
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
