"""现场采集契约（技术方案 5.1 智能体总表 ②）。

现场采集智能体把环境读数与图片识别归纳为带来源的现场事实。它的失败行为与分诊**相反**：
传感器无数据、图片识别不可用**不阻断问诊**——`data_available=False`，但流程继续，界面
标注环境说明，判定智能体据此降低确定性表述。分诊失败则回退通用流程（见 `triage.py`）。
这个差异必须落在契约结构上，不能只写在提示词里，否则"没有环境数据"会在界面上表现为
一段与有数据时毫无区别的判断。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

EnvMetric = Literal["air_temp", "air_humidity", "soil_temp", "soil_moisture", "light", "rainfall"]
EnvSource = Literal["sensor", "weather", "manual"]
EnvQuality = Literal["ok", "stale", "suspect", "missing"]
ObservationEnvSource = Literal["sensor", "weather", "manual", "none", "sensor_stale"]


class EnvReading(BaseModel):
    """一条环境读数。

    `value: float | None` 配 `quality` 字段，把"读到了但数据可疑"与"根本没读到"分开：
    前者 `value` 有值且 `quality="suspect"`，后者 `value=None` 且 `quality="missing"`。
    这与 `SlotValue` 区分 `unknown` 与 `missing` 是同一个设计原则——把不确定的形态显式
    表达出来，下游才能据以降低确定性表述，而不是把一个可疑数字当成事实使用。
    """

    model_config = ConfigDict(extra="forbid")

    metric: EnvMetric
    value: float | None
    unit: str
    source: EnvSource
    quality: EnvQuality = "ok"
    observed_at: datetime | None = None
    device_sn: str | None = None


class ImageCandidate(BaseModel):
    """一条图片识别候选。候选域由流程限定，`part` 对应受害部位。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    score: float
    part: str | None = None  # 叶片/茎/根/果实


class ObservationResult(BaseModel):
    """现场采集输出。数据不可用必须显式标记。"""

    model_config = ConfigDict(extra="forbid")

    readings: list[EnvReading] = []
    image_candidates: list[ImageCandidate] = []
    data_available: bool
    unavailable_reason: str | None = None
    env_source: ObservationEnvSource
    observed_facts: dict[str, str] = {}
    note: str | None = None  # 面向农户的环境说明

    @model_validator(mode="after")
    def _unavailable_requires_reason(self) -> "ObservationResult":
        """拿不到数据时必须说明原因（传感器离线 / 无该地块设备 / 图片识别失败）。

        没有原因时，运维与农户看到的是同一句"暂无数据"，无法判断是设备问题还是网络问题。
        """
        if not self.data_available and not self.unavailable_reason:
            raise ValueError("data_available=False 时必须给出 unavailable_reason")
        return self

    @model_validator(mode="after")
    def _env_note_required(self) -> "ObservationResult":
        """落实产品说明书 4.2.3 功能点 6：界面必须明确标注当前没有环境数据、以下判断
        基于农户描述。把这个要求做成契约必填字段，而不是靠接口层记得加，是唯一可靠的
        实现方式——接口层的"记得加"在第二次改动时就会失效。
        """
        if self.env_source in ("none", "sensor_stale") and not self.note:
            raise ValueError("env_source 为 none 或 sensor_stale 时必须提供 note")
        return self


__all__ = [
    "EnvMetric",
    "EnvQuality",
    "EnvReading",
    "EnvSource",
    "ImageCandidate",
    "ObservationEnvSource",
    "ObservationResult",
]
