# T2-15 现场采集智能体 FieldObserver

## 这一项要做什么

在 `app/agent/agents/field_observer.py` 中实现现场采集智能体，把环境读数与图片识别结果归纳为 `ObservationResult`，作为带来源、带质量标记的现场事实写入黑板状态。它是"看现场"这一步的实现者，产出供问诊提问与判定推理共同使用。

本智能体的核心要求是数据不可用必须显式标记，而不是静默返回空值。界面上"暂无环境数据，判断基于你的描述"这句话的触发条件，就来自这里的 `data_available` 与 `env_source` 字段。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`AgentContext`
- 依赖 T2-09：必须已提供 `ObservationResult`、`EnvReading`、`ImageCandidate`
- 依赖 T2-02：必须已提供 `ProcessConfig` 与 `slot_spec`
- 依赖 T2-56：必须已提供多模态结构化输出能力
- 外部环境：传感器读数来自 T5-08 的环境服务，图片识别来自垂类图像模型；二者在 T5 完成前以可注入的假实现提供
- 与 T2-50 的关系：地区回退匹配在知识库层，本智能体不负责

## 参考章节

- 技术方案 第5章 5.1 智能体总表（②现场采集，输入环境读数加图片，失败标记数据不可用）
- 技术方案 第4章 4.4 模型分层策略（现场采集：垂类图像模型加多模态，候选域按流程限定）
- 技术方案 第13章 13.2 问诊接口详细设计（`environmentNote` 在 `env_source` 为 none 或 sensor_stale 时必填）
- 技术方案 第5章 5.3.3 问诊契约（`confidence_note` 为数据缺失时的显式标注）
- 技术方案 第9章 9.1 流程配置模型（`offline_questions` 用于无传感器时的增强追问）
- 产品说明书 4.2.3 功能点 6（界面必须明确标注当前没有环境数据）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/field_observer.py` | `FieldObserverAgent` 与读数质量判定 |
| 新增 | `app/agent/agents/prompts/field_observer.py` | 提示词构造 |
| 新增 | `tests/agent/test_field_observer.py` | 读数质量、图片候选域、不可用标注的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `FieldObserverAgent` |

## 实现要点

### 类骨架

```python
class FieldObserverAgent(BaseAgent[ObservationResult]):
    name = "field_observer"
    output_schema = ObservationResult
    task_type = TaskType.OBSERVE
    failure_mode = FailureMode.DEGRADE          # 标记数据不可用，流程继续
    max_attempts = 2

    def __init__(self, gateway: ModelGateway, env_reader: EnvReader, image_reader: ImageReader) -> None:
        super().__init__(gateway)
        self._env = env_reader
        self._image = image_reader
```

`env_reader` 与 `image_reader` 是注入的只读接口，不是具体仓储。这样测试可以喂假读数，也不必连数据库或调用图像模型。

### 两条并行的数据来源

```python
    async def gather(self, state: ConsultationState, ctx: AgentContext) -> tuple[list[EnvReading], list[ImageCandidate]]:
        """读数与图片识别并行获取，任一路失败不影响另一路。"""
        readings_task = asyncio.create_task(self._collect_readings(state, ctx))
        images_task = asyncio.create_task(self._collect_images(state, ctx))
        readings, images = await asyncio.gather(readings_task, images_task, return_exceptions=True)

        if isinstance(readings, BaseException):
            log.warning("env readings unavailable", extra={"trace_id": ctx.trace_id, "reason": str(readings)})
            readings = []
        if isinstance(images, BaseException):
            log.warning("image recognition unavailable", extra={"trace_id": ctx.trace_id, "reason": str(images)})
            images = []
        return readings, images
```

用 `return_exceptions=True` 而不是让异常向上抛，是因为环境读数与图片识别是两条独立的数据通路：传感器离线不应导致图片识别结果被丢弃，反之亦然。任一路失败都要在最终结果里体现为对应的缺失标记。

### 读数质量判定

```python
STALE_MINUTES = 120
SUSPECT_RANGES: dict[str, tuple[float, float]] = {
    "air_temp": (-20.0, 55.0),
    "air_humidity": (0.0, 100.0),
    "soil_temp": (-10.0, 50.0),
    "soil_moisture": (0.0, 100.0),
    "light": (0.0, 200_000.0),
    "rainfall": (0.0, 500.0),
}


def classify_reading(raw: RawReading, now: datetime) -> EnvReading:
    """把原始读数归一为带质量标记的读数。"""
    lo, hi = SUSPECT_RANGES.get(raw.metric, (float("-inf"), float("inf")))
    quality: str = "ok"
    if raw.value is None:
        quality = "missing"
    elif not (lo <= raw.value <= hi):
        quality = "suspect"
    elif raw.observed_at is None or (now - raw.observed_at) > timedelta(minutes=STALE_MINUTES):
        quality = "stale"
    return EnvReading(
        metric=raw.metric, value=raw.value, unit=raw.unit, source=raw.source,
        quality=quality, observed_at=raw.observed_at, device_sn=raw.device_sn,
    )
```

判定顺序是"缺值、越界、过期"，先判最严重的一类。`stale` 的门限取两小时，依据是 P0 传感器的上报频率为十分钟级，两小时意味着十二个周期未上报。这个门限值属于工程参数而非农技内容，可以由执行者定，但必须写在模块常量里便于后续调整。

`SUSPECT_RANGES` 用物理可能范围而不是农学适宜范围：判定的目的是识别明显错误的读数（如温度 999 度），而不是判断读数是否适宜作物生长。后者属于闸门的 `R-REGION` 职责。

### env_source 的推导

```python
    def derive_env_source(self, readings: list[EnvReading]) -> str:
        usable = [r for r in readings if r.quality == "ok"]
        if not usable:
            if any(r.quality == "stale" for r in readings):
                return "sensor_stale"
            return "none"
        sources = {r.source for r in usable}
        if "sensor" in sources:
            return "sensor"
        if "weather" in sources:
            return "weather"
        return "manual"
```

`env_source` 是接口层 `environmentNote` 必填与否的判据（技术方案 13.2）。这里把它变成纯函数，避免在多个地方各自判断"算不算没有环境数据"。

`sensor_stale` 与 `none` 要分开：前者是"设备在但数据不新鲜"，后者是"完全没有环境数据"。界面上前者可以说"传感器数据较旧，以下判断以你的描述为主"，后者说"暂无环境数据"，二者给农户的观感不同。

### 图片候选域的限定

```python
    async def _collect_images(self, state: ConsultationState, ctx: AgentContext) -> list[ImageCandidate]:
        """图片识别候选域按流程限定，避免跨流程候选污染。"""
        image_urls = (state.get("observations") or {}).get("image_urls") or []
        if not image_urls:
            return []
        allowed = self._allowed_labels(ctx.process_config)
        raw = await self._image.recognize(image_urls, allowed_labels=allowed)
        return [ImageCandidate(label=c.label, score=c.score, part=c.part) for c in raw if c.label in allowed]

    def _allowed_labels(self, config: ProcessConfig) -> set[str]:
        """允许的标签来自流程的易混淆项候选与必问槽位选项。"""
        labels: set[str] = set()
        for pair in config.confusable_items:
            labels.update(pair.candidates)
        for spec in config.required_slots:
            if spec.options:
                labels.update(spec.options)
        return labels
```

技术方案 4.4 指出"候选域按流程限定，需可控"。因此图片识别的输出标签必须落在流程的易混淆项候选与槽位选项之内。茶流程的图片识别不应输出"稗草"这类杂草标签，否则判定智能体会拿到跨流程的线索。

`_allowed_labels` 的交集过滤发生在模型输出之后、进入 state 之前，属于代码层的硬约束，不依赖图像模型自律。

### 对图片结果的定位

图片识别结果**不直接构成证据**，它只填入槽位或作为观察事实：

```python
    def image_to_slots(self, candidates: list[ImageCandidate], config: ProcessConfig) -> list[SlotValue]:
        """把高置信图片候选写入对应槽位。仅当该槽位可由图片填充时。"""
        out: list[SlotValue] = []
        for spec in config.required_slots:
            if not spec.options or "image" not in spec.source_hint:
                continue
            for cand in candidates:
                if cand.label in spec.options:
                    out.append(SlotValue(
                        key=spec.key, value=cand.label, status="filled",
                        source="image", confidence=cand.score,
                    ))
                    break
        return out
```

只有 `source_hint` 含 `image` 的槽位才接受图片填充，且图片来源的优先级低于档案与用户回答。判定智能体仍然需要证据集才能真正下结论，图片只是加速信息收集。这个设计避免"看到叶子有斑就判定是某某病"这类单证据短路。

### 归纳现场事实

```python
    def _format_readings_for_prompt(self, readings: list[EnvReading]) -> str:
        if not readings:
            return "无环境读数"
        lines = []
        for r in readings:
            if r.quality == "ok":
                lines.append(f"{r.metric}={r.value}{r.unit}（{r.source}）")
            else:
                lines.append(f"{r.metric}=不可用（{r.quality}）")
        return "；".join(lines)
```

不可用的读数也要进入提示词，且明确标注为不可用。这样模型在归纳 `observed_facts` 时不会把缺失项当作"正常"，也不会编造数值。

`observed_facts` 的键用英文标识（如 `rain_last_3d`、`temp_avg_7d`），值用口语化中文描述。键要稳定，因为后续的问诊与判定节点会按固定键读取；值可以随流程调整。

### 降级实现

```python
    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> ObservationResult:
        """降级：标记数据不可用，流程继续，界面标注环境说明。"""
        return ObservationResult(
            readings=[], image_candidates=[],
            data_available=False,
            unavailable_reason=f"现场数据获取失败（{reason}）",
            env_source="none",
            observed_facts={},
            note="暂时拿不到环境数据，下面的判断主要根据您描述的情况。",
        )
```

降级返回的 `note` 是本任务与"数据不可用"相关的唯一面向农户的文案。文案本身要过适老化的字数约束（T3-07 与 T4-05），当前先按简洁口语写。

注意降级**不改 `stage`**：现场数据不可用不构成流程中断，主图继续进入问诊。这一点与分诊降级（改流程）不同。

### 无传感器时的增强追问

当 `env_source == "none"` 时，问诊环节应加载流程配置的 `offline_questions` 作为补充。本智能体不负责提问，但要在 `observed_facts` 或返回结果中留下可被读取的标记：

```python
    def offline_questions_needed(self, result: ObservationResult) -> bool:
        return result.env_source == "none"
```

T2-16 的问诊智能体据此决定是否把 `offline_questions` 纳入候选池。这个判断链路的入口在本项，实现在 T2-16 与 T2-17。

## 验收标准

- `pytest tests/agent/test_field_observer.py` 全部通过，含三类用例：读数为空时 `env_source="none"` 且 `note` 非空、读数过期时 `env_source="sensor_stale"`、图片标签落在候选域外时被过滤掉
- 断言 `classify_reading` 对温度 999 返回 `quality="suspect"`，对 `value=None` 返回 `quality="missing"`
- 断言环境读数异常与图片识别异常互不影响：mock 一路抛异常，另一路结果仍出现在 `ObservationResult` 中
- 断言降级返回的 `data_available` 为 False 且 `unavailable_reason` 非空
- 断言 `image_to_slots` 只对 `source_hint` 含 `image` 的槽位产出写入，且写入的 `source` 恒为 `"image"`
- 断言 `offline_questions_needed` 仅在 `env_source="none"` 时为 True
- 断言 `USE_MOCK_LLM=true` 下无真实网络请求
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
