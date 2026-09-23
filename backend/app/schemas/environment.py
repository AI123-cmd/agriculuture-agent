"""环境监测相关模型。"""

from pydantic import BaseModel


class EnvironmentSummary(BaseModel):
    temperature: float
    humidity: float
    soilStatus: str
    soilDetail: str
    lightStatus: str
    lightDetail: str
    lastUpdateAt: str


class MetricItem(BaseModel):
    id: str
    label: str
    value: str
    unit: str


class KVItem(BaseModel):
    label: str
    value: str


class DeviceItem(BaseModel):
    id: str
    name: str
    type: str
    status: int
    lastOnline: str


class EnvironmentDetail(BaseModel):
    metrics: list[MetricItem] = []
    airData: list[KVItem] = []
    soilData: list[KVItem] = []
    weatherData: list[KVItem] = []
    devices: list[DeviceItem] = []
    lastUpdateAt: str = ""
