"""环境监测相关模型（T5-08）。

字段名沿用既有环境接口的驼峰形式（`soilStatus` / `lastUpdateAt` / `lastOnline`），
因为这套出入参是给小程序前端消费的，与 T3、T2 阶段的接口风格一致；后端内部变量用蛇形，
转换发生在 schema 边界（`app/services/environment_service.py`）。

T5-08 新增三个模型：

- `BoardMetric`：看板的单个指标行，含数值、单位、来源质量与"正常/偏高/偏低"状态；
- `SourceBadge`：来源与新鲜度标注。`source` 的取值集合与 `EnvFacts.source` 完全相同
  （`sensor` / `sensor_stale` / `weather` / `none`）——同一份环境事实在首页看板与问诊接口
  必须给出一致的来源判断，否则会出现"看板说本地传感器、对话页说暂无环境数据"的矛盾；
- `EnvironmentBoard`：看板出参，复用既有 `DeviceItem`。

旧接口（`EnvironmentSummary` / `EnvironmentDetail`）保留原字段并新增 `sourceBadge` 供前端
渐进采用：内部数据源已改为环境事实采集，返回结构不变使小程序侧无需同步改动。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SourceBadge(BaseModel):
    """环境数据来源与新鲜度标注（产品说明书 4.4.2 的透明度要求）。

    `label` 取自 `app/domain/metrics.py` 的 `SOURCE_LABELS`，不在此拼写文案——同一句话在
    环境看板、问诊的 `environmentNote`、提示快照的来源标注三处出现，硬编码必然措辞分叉。
    """

    source: Literal["sensor", "sensor_stale", "weather", "none"]
    label: str
    # 仅 `sensor_stale` 非空：本地实测但不新，需告知"数据为 XX 小时前"。
    freshnessNote: str | None = None
    # 地区回退实际命中级，仅天气来源时给出（供界面标注"当前按 XX 市通用数据"）。
    regionHitLevel: str | None = None


class BoardMetric(BaseModel):
    """看板的单个指标行：数值 + 单位 + 质量 + 状态标注。

    `status` 只反映"是否落在阈值区间"，`quality` 反映"读数本身是否可信"。两者分开：
    `suspect`（超量程）与 `stale`（过期）的读数状态一律 `unknown` 而不是按数值算状态——
    一条 30 小时前的 22°C 不能标"正常"（不代表现在），一条 265°C 不能标"偏高"（是坏数据）。
    展示仍给原始数值，由前端配合 `quality` 决定是否弱化。
    """

    metric: str
    label: str
    value: float | None = None
    unit: str = ""
    status: str = "unknown"
    quality: str = "stale"
    observedAt: datetime | None = None


class DeviceItem(BaseModel):
    id: str
    name: str
    type: str
    status: int
    lastOnline: str


class EnvironmentBoard(BaseModel):
    """环境看板出参：指标行 + 来源标注 + 设备列表 + 最近更新时刻。"""

    metrics: list[BoardMetric] = []
    sourceBadge: SourceBadge
    devices: list[DeviceItem] = []
    lastUpdateAt: datetime | None = None


class EnvironmentSummary(BaseModel):
    temperature: float | None = None
    humidity: float | None = None
    soilStatus: str
    soilDetail: str
    lightStatus: str
    lightDetail: str
    lastUpdateAt: str
    sourceBadge: SourceBadge | None = None


class MetricItem(BaseModel):
    id: str
    label: str
    value: str
    unit: str


class KVItem(BaseModel):
    label: str
    value: str


class EnvironmentDetail(BaseModel):
    metrics: list[MetricItem] = []
    airData: list[KVItem] = []
    soilData: list[KVItem] = []
    weatherData: list[KVItem] = []
    devices: list[DeviceItem] = []
    lastUpdateAt: str = ""
    sourceBadge: SourceBadge | None = None


__all__ = [
    "BoardMetric",
    "DeviceItem",
    "EnvironmentBoard",
    "EnvironmentDetail",
    "EnvironmentSummary",
    "KVItem",
    "MetricItem",
    "SourceBadge",
]
