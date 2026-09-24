"""T2-15：现场采集智能体的读数质量、图片候选域与不可用标注。

用例分四组：

1. 环境来源：读数为空时报 `none`、读数过期时报 `sensor_stale`，两者都必须带面向农户的
   `note`（契约的 `_env_note_required` 只是兜底，本层要真的填）；
2. 读数质量：越界报 `suspect`、缺值报 `missing`、正常报 `ok`、超时报 `stale`；
3. 两路互不影响：任一路抛异常，另一路的结果仍要出现在 `ObservationResult` 里；
4. 图片与槽位：候选域外的标签被代码过滤，`image_to_slots` 只写 `source_hint` 含 `image`
   的槽位。

网关与两个 reader 都是替身：本智能体在这些依赖面前是**被调用方**，它内部走 HTTP 还是
走 mock、读数来自真实传感器还是假实现，都与采集行为无关。另有一条 `respx` 零请求断言，
保证它不会绕过网关自己发请求。
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import respx

from app.agent.agents.base import AgentContext
from app.agent.agents.field_observer import (
    STALE_MINUTES,
    FieldObserverAgent,
    RawReading,
    classify_reading,
)
from app.agent.contracts import ImageCandidate, ObservationResult
from app.agent.state import ConsultationState, ProcessId, SlotGroup, TaskType
from app.llm import ModelGateway, build_gateway
from app.llm.providers.base import ModelResponse
from app.observability import metrics
from app.process_config.loader import get_process_config
from app.process_config.models import ProcessConfig, SlotSpec

_MESSAGE = "我家茶树叶子发黄，这几天一直没下雨"
_IMAGE_URL = "https://example.invalid/leaf.jpg"


def _payload(**overrides: Any) -> dict[str, Any]:
    """模型侧的最小合法输出。读数与图片候选会被 `post_validate` 覆写，故此处留空。"""
    data: dict[str, Any] = {
        "readings": [],
        "image_candidates": [],
        "data_available": True,
        "unavailable_reason": None,
        "env_source": "sensor",
        "observed_facts": {"rain_last_3d": "近三天没有下雨"},
        "note": None,
    }
    data.update(overrides)
    return data


def _ok(**overrides: Any) -> ModelResponse:
    """网关成功时的响应：`parsed` 已是契约对象（适配器负责校验），不是裸字典。"""
    data = _payload(**overrides)
    return ModelResponse(
        content=json.dumps(data, ensure_ascii=False),
        parsed=ObservationResult.model_validate(data),
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=1,
    )


class _FakeGateway:
    """回放预设结果的网关替身。最后一次结果被后续所有调用复用。"""

    def __init__(self, results: list[ModelResponse | Exception]) -> None:
        self._results = results
        self.calls: list[tuple[object, list[dict], object]] = []

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.calls.append((task_type, list(messages), schema))
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def last_messages(self) -> list[dict]:
        return self.calls[-1][1]


class _FakeEnvReader:
    """环境读数只读接口的替身：给一批原始读数，或抛一个异常。"""

    def __init__(self, readings: list[RawReading] | Exception) -> None:
        self._readings = readings
        self.plot_ids: list[str | None] = []

    async def read(self, plot_id: str | None) -> list[RawReading]:
        self.plot_ids.append(plot_id)
        if isinstance(self._readings, Exception):
            raise self._readings
        return list(self._readings)


class _FakeImageReader:
    """图片识别接口的替身：给一批候选，或抛一个异常。"""

    def __init__(self, candidates: list[ImageCandidate] | Exception) -> None:
        self._candidates = candidates
        self.calls: list[tuple[list[str], set[str]]] = []

    async def recognize(self, image_urls: list[str], *, allowed_labels: set[str]) -> list[ImageCandidate]:
        self.calls.append((list(image_urls), set(allowed_labels)))
        if isinstance(self._candidates, Exception):
            raise self._candidates
        return list(self._candidates)


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(scope="module")
def tea_config() -> ProcessConfig:
    return get_process_config(ProcessId.TEA)


@pytest.fixture()
def ctx(tea_config: ProcessConfig) -> AgentContext:
    return AgentContext(
        process_config=tea_config,
        config_version=1,
        task_type=TaskType.OBSERVE,
        trace_id="t-1",
    )


def _agent(
    results: list[ModelResponse | Exception],
    readings: list[RawReading] | Exception = (),
    candidates: list[ImageCandidate] | Exception = (),
) -> tuple[FieldObserverAgent, _FakeGateway, _FakeEnvReader, _FakeImageReader]:
    gateway = _FakeGateway(results)
    env = _FakeEnvReader(readings)
    image = _FakeImageReader(candidates)
    agent = FieldObserverAgent(cast(ModelGateway, gateway), env, image)
    return agent, gateway, env, image


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 1,
        "slot_round": 0,
        "messages": [{"role": "user", "content": _MESSAGE}],
    }
    state.update(overrides)
    return state


def _fresh(**overrides: Any) -> RawReading:
    """一条刚上报的读数。`observed_at` 取当前时刻，避免被误判为过期。"""
    data: dict[str, Any] = {
        "metric": "air_temp",
        "value": 22.5,
        "unit": "℃",
        "source": "sensor",
        "observed_at": datetime.now(UTC),
    }
    data.update(overrides)
    return RawReading(**data)


# ---- 第一组：环境来源 ----


async def test_empty_readings_mark_env_source_none(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """读数为空时 `env_source="none"` 且给出面向农户的 `note`。"""
    agent, _, _, _ = _agent([_ok()], readings=[], candidates=[])

    result = await agent.run(_state(observations={"image_urls": []}), ctx)

    assert result.env_source == "none"
    assert result.note
    assert result.data_available is False
    assert result.unavailable_reason
    # 模型归纳的观察事实保留：数据采集由代码负责，事实归纳仍由模型负责。
    assert result.observed_facts == {"rain_last_3d": "近三天没有下雨"}


async def test_stale_readings_mark_sensor_stale(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """只有过期读数时报 `sensor_stale`，与"完全没有环境数据"区分开。"""
    stale = _fresh(observed_at=datetime.now(UTC) - timedelta(minutes=STALE_MINUTES + 30))
    agent, _, _, _ = _agent([_ok(env_source="sensor")], readings=[stale])

    result = await agent.run(_state(), ctx)

    assert result.env_source == "sensor_stale"
    assert result.note
    assert result.data_available is False
    assert result.unavailable_reason


async def test_fresh_readings_mark_sensor_source(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """有可用传感器读数时 `env_source="sensor"`，不需要环境说明。"""
    agent, _, env, _ = _agent([_ok(env_source="none", note="模型编的说明")], readings=[_fresh()])

    result = await agent.run(_state(farm_plot_id="plot-1"), ctx)

    assert result.env_source == "sensor"
    assert result.data_available is True
    # 有数据时不得留下"不可用"的痕迹，否则下游会据此降低确定性表述。
    assert result.unavailable_reason is None
    assert env.plot_ids == ["plot-1"]


# ---- 第二组：读数质量 ----


def test_classify_reading_flags_out_of_range_and_missing() -> None:
    """越界报 `suspect`、缺值报 `missing`：两者都不能被当成可用读数。"""
    now = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)

    suspect = classify_reading(_fresh(value=999.0, observed_at=now), now)
    missing = classify_reading(_fresh(value=None, observed_at=now), now)
    ok = classify_reading(_fresh(value=22.5, observed_at=now), now)
    stale = classify_reading(_fresh(observed_at=now - timedelta(minutes=STALE_MINUTES + 1)), now)

    assert suspect.quality == "suspect"
    assert suspect.value == 999.0
    assert missing.quality == "missing"
    assert missing.value is None
    assert ok.quality == "ok"
    assert stale.quality == "stale"


def test_classify_reading_reports_suspect_before_stale() -> None:
    """既越界又过期时报 `suspect`：可疑数值比陈旧更危险，它会被下游当成事实使用。"""
    now = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)

    reading = classify_reading(_fresh(value=999.0, observed_at=now - timedelta(days=2)), now)

    assert reading.quality == "suspect"


def test_offline_questions_only_when_none() -> None:
    """`offline_questions_needed` 仅在完全没有环境数据时为 True。

    传感器数据陈旧时问的是"传感器数据较旧"，不是"没有传感器"：此时按设备已装但离线处理
    （排查设备），而不是加载无传感器专用追问。
    """
    agent, _, _, _ = _agent([_ok()])

    def result(env_source: str) -> ObservationResult:
        data_available = env_source not in ("none", "sensor_stale")
        return ObservationResult.model_validate(
            {
                "data_available": data_available,
                "unavailable_reason": None if data_available else "无该地块的环境设备",
                "env_source": env_source,
                "note": None if data_available else "暂时拿不到环境数据，下面的判断主要根据您描述的情况。",
            }
        )

    assert agent.offline_questions_needed(result("none")) is True
    assert agent.offline_questions_needed(result("sensor_stale")) is False
    assert agent.offline_questions_needed(result("sensor")) is False
    assert agent.offline_questions_needed(result("manual")) is False


# ---- 第三组：两路数据互不影响 ----


async def test_env_failure_does_not_drop_image_candidates(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """传感器离线不应导致图片识别结果被丢弃。"""
    candidates = [ImageCandidate(label="叶片", score=0.91)]
    agent, _, _, _ = _agent([_ok()], readings=ValueError("传感器离线"), candidates=candidates)

    result = await agent.run(_state(observations={"image_urls": [_IMAGE_URL]}), ctx)

    assert result.readings == []
    assert [c.label for c in result.image_candidates] == ["叶片"]
    assert result.data_available is True


async def test_image_failure_does_not_drop_readings(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """图片识别失败不应导致环境读数被丢弃。"""
    agent, _, _, _ = _agent([_ok()], readings=[_fresh()], candidates=ValueError("图像模型不可用"))

    result = await agent.run(_state(observations={"image_urls": [_IMAGE_URL]}), ctx)

    assert [r.metric for r in result.readings] == ["air_temp"]
    assert result.readings[0].quality == "ok"
    assert result.image_candidates == []


# ---- 第四组：图片候选域与槽位 ----


async def test_image_labels_outside_candidate_domain_are_filtered(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """茶流程不接受"稗草"这类跨流程标签，它不得进入 `ObservationResult`。"""
    candidates = [
        ImageCandidate(label="稗草", score=0.95),
        ImageCandidate(label="叶片", score=0.62),
    ]
    agent, _, _, image = _agent([_ok()], candidates=candidates)

    result = await agent.run(_state(observations={"image_urls": [_IMAGE_URL]}), ctx)

    assert [c.label for c in result.image_candidates] == ["叶片"]
    # 候选域来自流程配置的易混淆项与槽位选项，随流程变化；识别接口收到的正是这份候选域。
    passed_labels = image.calls[0][1]
    assert "稗草" not in passed_labels
    assert "叶片" in passed_labels


def test_image_to_slots_only_fills_image_slots(tea_config: ProcessConfig) -> None:
    """只有 `source_hint` 含 `image` 且声明了选项的槽位接受图片写入，来源恒为 `image`。"""
    agent, _, _, _ = _agent([_ok()])
    config = tea_config.model_copy(
        update={
            "required_slots": [
                SlotSpec(
                    key="photo_slot",
                    label="照片识别",
                    display_label="照片里看到什么",
                    group=SlotGroup.SEEDLING,
                    required=False,
                    owner="field_observer",
                    source_hint=["image"],
                    options=["叶片", "枝干"],
                    ask_order=10,
                ),
                SlotSpec(
                    key="profile_slot",
                    label="非图片槽位",
                    display_label="其他",
                    group=SlotGroup.SEEDLING,
                    required=False,
                    owner="field_observer",
                    source_hint=["profile"],
                    options=["叶片"],
                    ask_order=20,
                ),
            ]
        }
    )
    candidates = [ImageCandidate(label="叶片", score=0.88), ImageCandidate(label="枝干", score=0.42)]

    slots = agent.image_to_slots(candidates, config)

    assert [s.key for s in slots] == ["photo_slot"]
    assert slots[0].value == "叶片"
    assert slots[0].source == "image"
    assert slots[0].status == "filled"
    assert slots[0].confidence == 0.88


def test_image_to_slots_ignores_undeclared_labels(tea_config: ProcessConfig) -> None:
    """不在槽位选项里的候选不写入槽位：图片结果不直接构成证据。"""
    agent, _, _, _ = _agent([_ok()])
    config = tea_config.model_copy(
        update={
            "required_slots": [
                SlotSpec(
                    key="photo_slot",
                    label="照片识别",
                    display_label="照片里看到什么",
                    group=SlotGroup.SEEDLING,
                    required=False,
                    owner="field_observer",
                    source_hint=["image"],
                    options=["叶片"],
                    ask_order=10,
                )
            ]
        }
    )

    slots = agent.image_to_slots([ImageCandidate(label="枝干", score=0.99)], config)

    assert slots == []


# ---- 降级与提示词 ----


async def test_degrade_marks_data_unavailable(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """采集与模型双双失败时降级：`data_available=False` 且给出 `unavailable_reason`。"""
    agent, gateway, _, _ = _agent(
        [ValueError("网关未装配")],
        readings=ValueError("传感器离线"),
        candidates=ValueError("图像模型不可用"),
    )

    result = await agent.run(_state(observations={"image_urls": [_IMAGE_URL]}), ctx)

    assert len(gateway.calls) == 1
    assert result.data_available is False
    assert result.unavailable_reason and "ValueError" in result.unavailable_reason
    assert result.env_source == "none"
    assert result.note
    assert result.readings == []
    assert result.image_candidates == []
    assert metrics.counter_value("agent_degraded_total", tags={"agent": "field_observer", "mode": "degrade"}) == 1


async def test_prompt_marks_unavailable_readings(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """不可用读数进入提示词时显式标注为不可用，模型才不会把缺失项当作正常。"""
    stale = _fresh(value=999.0)
    agent, gateway, _, _ = _agent([_ok()], readings=[stale])

    await agent.run(_state(), ctx)

    user_content = gateway.last_messages[-1]["content"]
    assert "air_temp=不可用（suspect）" in user_content
    assert _MESSAGE in user_content
    system_content = gateway.last_messages[0]["content"]
    assert "不得编造任何数值" in system_content


# ---- mock 模式与零网络请求 ----


async def test_run_under_mock_returns_contract_object(tea_config: ProcessConfig, ctx: AgentContext) -> None:
    """`USE_MOCK_LLM=true` 下 `run` 返回满足契约的对象（读数仍以采集结果为准）。"""
    agent = FieldObserverAgent(
        build_gateway(),
        _FakeEnvReader([_fresh()]),
        _FakeImageReader([ImageCandidate(label="叶片", score=0.5)]),
    )

    result = await agent.run(_state(observations={"image_urls": [_IMAGE_URL]}), ctx)

    assert isinstance(result, ObservationResult)
    assert result.env_source == "sensor"
    assert [r.metric for r in result.readings] == ["air_temp"]


async def test_no_network_request_during_run(
    tea_config: ProcessConfig, ctx: AgentContext, respx_mock: respx.Router
) -> None:
    """现场采集全程不得发起任何 HTTP 请求：读数与图片都经注入接口取得。"""
    agent, gateway, _, _ = _agent([_ok()], readings=[_fresh()], candidates=[ImageCandidate(label="叶片", score=0.5)])

    await agent.run(_state(observations={"image_urls": [_IMAGE_URL]}), ctx)

    assert len(gateway.calls) == 1
    assert respx_mock.calls == []
