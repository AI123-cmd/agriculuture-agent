"""现场采集智能体：把环境读数与图片识别归纳为带质量标记的现场事实（T2-15）。

它是"看现场"这一步的实现者，产出同时供问诊提问与判定推理使用。与分诊相反，它的失败
**不阻断流程**：传感器无数据、图片识别不可用都只是 `data_available=False` 加一句面向农户
的说明，问诊照常继续。界面上"暂时拿不到环境数据，下面的判断主要根据您描述的情况"这句话
的触发条件，就是本模块的 `env_source` 与 `note`。

两处判定刻意不用模型：

- **读数质量**（`classify_reading`）：缺值、越界、过期三类是明确的区间与时间比较。让模型判
  只会引入不确定性，而"把 999 度当成有效温度"这类错误会直接进入判定推理。
- **`env_source`**（`derive_env_source`）：它是接口层 `environmentNote` 必填与否的判据
  （技术方案 13.2）。做成纯函数，才不会在多个地方各自判断"算不算没有环境数据"。

`env_reader` 与 `image_reader` 是注入的只读接口而不是仓储：环境服务（T5-08）与图像模型
尚未就位，注入让"没有传感器"也能被完整单测，且守 L2（智能体层不碰 ORM）。
"""

import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.contracts import ImageCandidate, ObservationResult
from app.agent.contracts.observation import (
    EnvMetric,
    EnvQuality,
    EnvReading,
    EnvSource,
    ObservationEnvSource,
)
from app.agent.state import ConsultationState, SlotValue, TaskType
from app.llm import ModelGateway
from app.observability import log
from app.process_config.models import ProcessConfig

# 读数超过此时长视为过期。P0 传感器的上报频率为十分钟级，两小时意味着十二个周期未上报。
# 它属工程参数而非农技内容，故以模块常量给出，便于后续按现场设备情况调整。
STALE_MINUTES = 120

# 物理可能范围，不是农学适宜范围。判定的目的是识别明显错误的读数（如温度 999 度），
# 而不是判断读数是否适宜作物生长——后者属闸门 `R-REGION` 的职责。
SUSPECT_RANGES: dict[str, tuple[float, float]] = {
    "air_temp": (-20.0, 55.0),
    "air_humidity": (0.0, 100.0),
    "soil_temp": (-10.0, 50.0),
    "soil_moisture": (0.0, 100.0),
    "light": (0.0, 200_000.0),
    "rainfall": (0.0, 500.0),
}

# 面向农户的环境说明。文案简短口语，符合适老化约束（T3-07）。
_NOTE_NONE = "暂时拿不到环境数据，下面的判断主要根据您描述的情况。"
_NOTE_STALE = "传感器数据比较旧，下面的判断主要根据您描述的情况。"


@dataclass(frozen=True, slots=True)
class RawReading:
    """环境服务的原始读数。质量标记由 `classify_reading` 判定，不在此处声明。"""

    metric: EnvMetric
    value: float | None
    unit: str
    source: EnvSource
    observed_at: datetime | None = None
    device_sn: str | None = None


def classify_reading(raw: RawReading, now: datetime) -> EnvReading:
    """把原始读数归一为带质量标记的读数。

    判定顺序是"缺值、越界、过期"，先判最严重的一类：一个既越界又过期的读数报 `suspect`
    ——可疑的数值比陈旧更危险，它会被下游当成事实使用。
    """
    lo, hi = SUSPECT_RANGES.get(raw.metric, (float("-inf"), float("inf")))
    quality: EnvQuality = "ok"
    if raw.value is None:
        quality = "missing"
    elif not (lo <= raw.value <= hi):
        quality = "suspect"
    elif raw.observed_at is None or (now - raw.observed_at) > timedelta(minutes=STALE_MINUTES):
        quality = "stale"
    return EnvReading(
        metric=raw.metric,
        value=raw.value,
        unit=raw.unit,
        source=raw.source,
        quality=quality,
        observed_at=raw.observed_at,
        device_sn=raw.device_sn,
    )


class EnvReader(Protocol):
    """环境读数只读接口。T5-08 提供实现，本阶段由假实现注入。"""

    async def read(self, plot_id: str | None) -> list[RawReading]:
        """读某地块的环境读数。无设备、离线等情况抛异常或返回空列表。"""


class ImageReader(Protocol):
    """图片识别只读接口。垂类图像模型提供实现，本阶段由假实现注入。"""

    async def recognize(self, image_urls: list[str], *, allowed_labels: set[str]) -> list[ImageCandidate]:
        """识别图片。`allowed_labels` 是候选域提示，最终过滤仍由本模块完成。"""


# 采集结果经 contextvar 传给 `build_messages`：基类的 `build_messages` 是同步方法（这是
# T2-13 的契约，便于单测直接调用），而采集是异步的。用 contextvar 而不是实例属性，是因为
# 节点函数在多个会话间并发执行——asyncio 任务各自持有上下文副本，实例属性会被互相覆盖。
_gathered: ContextVar[tuple[list[EnvReading], list[ImageCandidate]] | None] = ContextVar(
    "field_observer_gathered", default=None
)


class FieldObserverAgent(BaseAgent[ObservationResult]):
    """现场采集：读环境读数、读图片识别结果、归纳现场事实。"""

    name = "field_observer"
    output_schema = ObservationResult
    task_type = TaskType.OBSERVE
    failure_mode = FailureMode.DEGRADE  # 标记数据不可用，流程继续
    max_attempts = 2

    def __init__(self, gateway: ModelGateway, env_reader: EnvReader, image_reader: ImageReader) -> None:
        super().__init__(gateway)
        self._env = env_reader
        self._image = image_reader

    async def run(self, state: ConsultationState, ctx: AgentContext) -> ObservationResult:
        """先并行采集两路数据，再走基类的调用、校验与降级。

        采集放在 `run` 而不是 `build_messages` 里，是因为后者是同步的；采集结果经 contextvar
        传递，重试时复用同一批数据——同一轮问诊里重复读传感器没有意义。
        """
        readings, images = await self.gather(state, ctx)
        token = _gathered.set((readings, images))
        try:
            return await super().run(state, ctx)
        finally:
            _gathered.reset(token)

    # ---- 两条并行的数据来源 ----

    async def gather(
        self, state: ConsultationState, ctx: AgentContext
    ) -> tuple[list[EnvReading], list[ImageCandidate]]:
        """读数与图片识别并行获取，任一路失败不影响另一路。

        用 `return_exceptions=True` 而不是让异常向上抛：环境读数与图片识别是两条独立的数据
        通路——传感器离线不应导致图片识别结果被丢弃，反之亦然。任一路失败都要在最终结果里
        体现为对应的缺失标记。
        """
        readings_task = asyncio.create_task(self._collect_readings(state, ctx))
        images_task = asyncio.create_task(self._collect_images(state, ctx))
        readings, images = await asyncio.gather(readings_task, images_task, return_exceptions=True)
        if isinstance(readings, BaseException):
            log.warning("env readings unavailable", reason=str(readings), trace_id=ctx.trace_id)
            readings = []
        if isinstance(images, BaseException):
            log.warning("image recognition unavailable", reason=str(images), trace_id=ctx.trace_id)
            images = []
        return readings, images

    async def _collect_readings(self, state: ConsultationState, ctx: AgentContext) -> list[EnvReading]:
        """读环境读数并逐条判定质量。`now` 取一次，保证同一批读数的判定基准一致。"""
        raw = await self._env.read(state.get("farm_plot_id"))
        now = datetime.now(UTC)
        return [classify_reading(item, now) for item in raw]

    async def _collect_images(self, state: ConsultationState, ctx: AgentContext) -> list[ImageCandidate]:
        """图片识别候选域按流程限定，避免跨流程候选污染。"""
        # `image_url` 是接口层注入的声明键（T2-33）；`observations.image_urls` 为历史写法保留，
        # 两者都接受是为了不破坏既有单测，新代码一律走前者。
        url = state.get("image_url")
        image_urls = [url] if url else list((state.get("observations") or {}).get("image_urls") or [])
        if not image_urls:
            return []
        return await self.recognize_images(image_urls, ctx.process_config)

    async def recognize_images(self, image_urls: list[str], config: ProcessConfig) -> list[ImageCandidate]:
        """识别图片并按流程候选域过滤。

        现场采集与追问拍照作答共用同一条识别路径：候选域过滤在模型输出之后、进入 state
        之前，是代码层的硬约束，不依赖图像模型自律。追问作答复用此方法，两条路径对
        "哪些标签算数"因此给出同一答案。
        """
        allowed = self._allowed_labels(config)
        raw = await self._image.recognize(image_urls, allowed_labels=allowed)
        return [ImageCandidate(label=c.label, score=c.score, part=c.part) for c in raw if c.label in allowed]

    def _allowed_labels(self, config: ProcessConfig) -> set[str]:
        """允许的标签来自流程的易混淆项候选与必问槽位选项。

        茶流程的图片识别不应输出"稗草"这类杂草标签，否则判定智能体会拿到跨流程的线索。
        """
        labels: set[str] = set()
        for pair in config.confusable_items:
            labels.update(pair.candidates)
        for spec in config.required_slots:
            if spec.options:
                labels.update(spec.options)
        return labels

    # ---- env_source 的推导 ----

    def derive_env_source(self, readings: list[EnvReading]) -> ObservationEnvSource:
        """据可用读数推导环境数据来源。

        `sensor_stale` 与 `none` 必须分开：前者是"设备在但数据不新鲜"，后者是"完全没有环境
        数据"。界面上前者可以说"传感器数据较旧"，后者说"暂无环境数据"，农户观感不同。
        """
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

    # ---- 图片结果定位 ----

    def image_to_slots(self, candidates: list[ImageCandidate], config: ProcessConfig) -> list[SlotValue]:
        """把图片候选写入对应槽位。仅当该槽位可由图片填充时。

        只有 `source_hint` 含 `image` 的槽位才接受图片填充，且图片的优先级低于档案与用户
        回答（见 `SOURCE_PRIORITY`）。图片识别结果**不直接构成证据**：判定仍需证据集，图片
        只是加速信息收集，这避免了"看到叶子有斑就判定是某某病"这类单证据短路。
        """
        out: list[SlotValue] = []
        for spec in config.required_slots:
            if not spec.options or "image" not in spec.source_hint:
                continue
            for cand in candidates:
                if cand.label in spec.options:
                    out.append(
                        SlotValue(
                            key=spec.key,
                            value=cand.label,
                            status="filled",
                            source="image",
                            confidence=cand.score,
                        )
                    )
                    break
        return out

    # ---- 输入构造 ----

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词：读数与图片候选结构化注入，不可用项显式标注为不可用。

        `observed_facts` 的键要稳定（如 `rain_last_3d`），因为后续问诊与判定节点按固定键
        读取；值用口语化中文，可随流程调整。这条写进提示词，避免模型自创键名。
        """
        readings, images = _gathered.get() or ([], [])
        system = [
            f"你是{ctx.process_config.role_definition}的现场查看模块，负责归纳现场事实。",
            "你必须输出符合以下 JSON Schema 的内容：",
            json.dumps(ObservationResult.model_json_schema(), ensure_ascii=False),
            "硬规则：不得编造任何数值。读数标注为不可用时，不得推测其取值。",
            "observed_facts 的键用英文短标识（如 rain_last_3d、temp_avg_7d），值用口语化中文。",
        ]
        user = [
            f"环境读数：{self._format_readings_for_prompt(readings)}",
            f"图片识别候选：{self._format_candidates_for_prompt(images)}",
            f"农户描述：{self._current_message_text(state)}",
        ]
        return [
            {"role": "system", "content": "\n".join(system)},
            {"role": "user", "content": "\n".join(user)},
        ]

    def _format_readings_for_prompt(self, readings: list[EnvReading]) -> str:
        """不可用的读数也要进入提示词，且明确标注为不可用。

        这样模型归纳 `observed_facts` 时不会把缺失项当作"正常"，也不会为了让句子通顺而补一个
        像样的数值。
        """
        if not readings:
            return "无环境读数"
        lines: list[str] = []
        for r in readings:
            if r.quality == "ok":
                lines.append(f"{r.metric}={r.value}{r.unit}（{r.source}）")
            else:
                lines.append(f"{r.metric}=不可用（{r.quality}）")
        return "；".join(lines)

    @staticmethod
    def _format_candidates_for_prompt(candidates: list[ImageCandidate]) -> str:
        if not candidates:
            return "无图片识别结果"
        return "；".join(f"{c.label}（{c.score:.2f}）" for c in candidates)

    @staticmethod
    def _current_message_text(state: ConsultationState) -> str:
        for message in reversed(list(state.get("messages") or [])):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else ""
        return ""

    # ---- 契约之外的字段覆写 ----

    async def post_validate(
        self, result: ObservationResult, state: ConsultationState, ctx: AgentContext
    ) -> ObservationResult:
        """读数、图片候选与环境来源一律以代码采集结果为准，并补齐缺失标注。

        不让模型回填这三项是刻意的：读数与图片是采集事实，模型转述会失真（丢单位、改数值），
        而 `env_source` 是界面必填标注的判据，必须与采集结果严格一致。
        """
        readings, images = _gathered.get() or ([], [])
        env_source = self.derive_env_source(readings)
        data_available = any(r.quality == "ok" for r in readings) or bool(images)
        update: dict = {
            "readings": readings,
            "image_candidates": images,
            "env_source": env_source,
            "data_available": data_available,
        }
        if env_source in ("none", "sensor_stale"):
            update["note"] = result.note or (_NOTE_STALE if env_source == "sensor_stale" else _NOTE_NONE)
        # 有可用数据时不留"不可用原因"：它进了 state 就可能被下游当成"这次没数据"的判据。
        update["unavailable_reason"] = None if data_available else self._unavailable_reason(result, readings)
        return result.model_copy(update=update)

    @staticmethod
    def _unavailable_reason(result: ObservationResult, readings: list[EnvReading]) -> str:
        """数据不可用时给运维一个可分辨的原因，而不是笼统的"暂无数据"。

        降级路径（`degrade`）给的原因更具体，优先保留它。
        """
        if result.unavailable_reason:
            return result.unavailable_reason
        if readings:
            return "环境读数均不可用"
        return "无该地块的环境设备，也未提供可用图片"

    # ---- 无传感器时的增强追问 ----

    def offline_questions_needed(self, result: ObservationResult) -> bool:
        """无环境数据时问诊应加载流程配置的 `offline_questions`（T2-16/T2-17 消费）。

        本智能体不负责提问，只留下这个可被判定的标记。
        """
        return result.env_source == "none"

    # ---- 降级 ----

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> ObservationResult:
        """降级：标记数据不可用，流程继续，界面标注环境说明。

        注意降级**不改 `stage`**：现场数据不可用不构成流程中断，主图继续进入问诊。这一点与
        分诊降级（改流程、回退 general）不同。
        """
        return ObservationResult(
            readings=[],
            image_candidates=[],
            data_available=False,
            unavailable_reason=f"现场数据获取失败（{reason}）",
            env_source="none",
            observed_facts={},
            note=_NOTE_NONE,
        )


__all__ = [
    "STALE_MINUTES",
    "SUSPECT_RANGES",
    "EnvReader",
    "FieldObserverAgent",
    "ImageReader",
    "RawReading",
    "classify_reading",
]
