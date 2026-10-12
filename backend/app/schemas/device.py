"""设备接入相关出入参（T5-03 / T5-09，技术方案 13.4）。

字段名保持方案里的驼峰形式，因为这套出入参是给小程序前端消费的，与 T3、T2 阶段的接口风格一致
（`ChatResponse` 的 `sessionId` / `stageLabel` 同为驼峰）。后端内部变量用蛇形，转换发生在 schema 边界。

`BindRequest` 的 `deviceSn` 用 `min_length=1, max_length=60` 做长度约束：库列是 `String(60)`，
超长必须在进入业务逻辑前拦下（FastAPI 对 schema 校验失败返回 422）。字符级规则（不含空白与非法
字符）在服务层判定，因为那属于业务规则而非契约长度，失败返回 400 并带中文提示。

`BindResponse` 在方案 13.4 的四个字段之外补两个下发字段：`reportUrl`（设备上报地址）与
`deviceSecret`（明文密钥，只在绑定时出现一次）。农户要把这两个值抄进设备或网关注入配置。
`deviceSecret` 在重复绑定的幂等分支返回 `None`——重复点击时若刷新密钥，之前已配进设备的密钥会
立即失效，表现为"设备突然不上报了"。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BindRequest(BaseModel):
    """设备绑定入参：农户扫码或手填的设备编号，可选关联地块。"""

    model_config = ConfigDict(extra="forbid")

    deviceSn: str = Field(min_length=1, max_length=60)
    # 可为空：产品说明书 4.4 把"地块"作为可选维度，农户可以只绑设备不指定地块。
    farmEntryId: str | None = None


class BindResponse(BaseModel):
    """设备绑定结果：下一步引导 + 一次性的密钥与上报地址下发。"""

    deviceId: str
    vendorTemplateMatched: bool
    templateId: str | None = None
    # 命中模板走确认页，未命中走粘贴页；前端据它决定渲染哪一页。
    nextStep: str = "provide_sample"
    # 设备上报地址（配置进设备或 DTU）。
    reportUrl: str | None = None
    # 明文设备密钥，配置为 `X-Device-Secret` 请求头；幂等分支为 `None`。
    deviceSecret: str | None = None


class ParseSampleRequest(BaseModel):
    """样例报文解析入参（T5-04）。

    `payload` 用 `str` 而非 `dict`：农户从设备说明书或抓包工具复制的是文本，允许带尾随换行、
    全角引号、单引号键这类脏字符。当字符串接收并在服务内清洗，比在 schema 层要求合法 JSON 更
    宽容，也更符合"农户粘贴"的实际形态。
    """

    model_config = ConfigDict(extra="forbid")

    deviceId: str
    payload: str


class DetectedField(BaseModel):
    """系统解析出的单个字段（T5-04，供农户确认或改选）。

    `sampleValue` 是字符串而非数字：报文可能有 `"26.3"`（带引号）、`"ON"`（枚举）、`"1"`（布尔），
    统一按字符串展示，避免解析期就因类型不符拒绝整条报文。数值化发生在推算换算参数时。

    `inferredMetric` 可为空：设备状态字段（`battery`/`rssi`）不对应任何环境指标，给它们错误的
    推断比留空更糟（农户会照着点确认）。此时 `candidates` 仍给出可选项，"忽略此字段"排在末位。
    """

    rawField: str
    sampleValue: str
    inferredMetric: str | None = None
    candidates: list[str] = []
    confidence: float = 0.0


class ParseSampleResponse(BaseModel):
    """样例报文解析结果：解析成功与否 + 字段推断列表 + 失败时的友好提示。"""

    parsed: bool
    fields: list[DetectedField] = []
    inferError: str | None = None


class MappingItem(BaseModel):
    """单条字段映射（T5-05，三步接入第三步的农户点选结果）。

    `scaleFactor` / `offsetValue` / `dataType` / `sampleRaw` 由系统在解析阶段给出，前端可原样
    回传，但**后端一律覆盖**：换算参数是可推导的（由样例值试算校验），单位是展示属性，都不该
    成为保存失败的由头。`extra="forbid"` 不收未声明字段，防止前端夹带绕过校验的键。
    """

    model_config = ConfigDict(extra="forbid")

    rawField: str
    standardMetric: str
    unit: str = ""
    scaleFactor: float = 1.0
    offsetValue: float = 0.0
    dataType: str = "float"
    sampleRaw: str | None = None


class MappingSaveRequest(BaseModel):
    """字段映射保存入参：农户在确认页上点选的**完整**映射视图。

    整体替换语义：请求体即该设备映射的最终状态，未出现的字段视为"农户忽略了"。因此前端不需要
    维护差异（删除要显式发指令），避免漏发删除而留下幽灵映射。
    """

    model_config = ConfigDict(extra="forbid")

    deviceId: str
    mappings: list[MappingItem]


class MappingPreview(BaseModel):
    """保存后预览的一行：原始字段 → 换算后的标准读数。

    值由后端用保存后的 `field_mapping` 实跑一遍 `parse_payload` 得出，而非另算一份，使预览与
    实际上报链路的口径完全一致（T5-05 与 T5-06 共享该函数）。
    """

    rawField: str
    sampleRaw: str
    standardMetric: str
    value: float
    unit: str


class MappingSaveResponse(BaseModel):
    """字段映射保存结果：保存条数 + 被忽略字段 + 非阻断提示 + 换算预览。"""

    deviceId: str
    saved: int
    skipped: list[str] = []
    warnings: list[str] = []
    preview: list[MappingPreview] = []


class DeviceListItem(BaseModel):
    """设备管理列表的一行（T5-09）。

    `onlineState` 用字符串枚举（`online` / `offline` / `fault`）而非旧 `device.status` 的整数
    编码：全局约定要求状态字段用字符串枚举，且 `device_config.online_status` 列本身就是字符串。
    判据由最近上报时间反推（`_online_state`），不要求设备实现心跳。

    `silenceNote` 只在 `onlineState != "online"` 时非空，把"断了多久"直说给农户，避免他对着一个
    "离线"标签猜测。`mappedFields` 是"这台设备接好了没有"的判据：绑定成功但映射没保存的设备会
    持续为 0，上报时返回 `mapping_missing`（T5-06），农户据此知道接入没走完。
    """

    id: str
    deviceSn: str
    deviceName: str | None = None
    farmEntryId: str | None = None
    onlineState: str
    lastReportAt: datetime | None = None
    silenceNote: str | None = None
    mappedFields: int = 0
    boundAt: datetime


class DeviceListResponse(BaseModel):
    """设备列表出参（T5-09）。"""

    devices: list[DeviceListItem] = []


__all__ = [
    "BindRequest",
    "BindResponse",
    "DeviceListItem",
    "DeviceListResponse",
    "DetectedField",
    "MappingItem",
    "MappingPreview",
    "MappingSaveRequest",
    "MappingSaveResponse",
    "ParseSampleRequest",
    "ParseSampleResponse",
]
