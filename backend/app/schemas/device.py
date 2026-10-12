"""设备接入相关出入参（T5-03，技术方案 13.4）。

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


__all__ = ["BindRequest", "BindResponse"]
