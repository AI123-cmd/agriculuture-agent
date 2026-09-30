"""提示与触达链路的字符串枚举（T3-01，技术方案 12.5）。

六个枚举供 `prompt_snapshot` / `prompt_item` / `reach_record` 三张表的状态字段使用，
与 `app.gate.enums.ProcessId` 同一风格：继承 `str, Enum`，取值即落库字符串。`str` 混入
让 `Severity.URGENT == "urgent"` 成立，避免在 JSON 契约、JSONB 列与数据库字符串列之间
反复做类型转换（全局约定 四、契约与编码约束）。

**刻意不用 PostgreSQL 原生 ENUM 类型**：原生枚举新增取值必须走 `ALTER TYPE`，而本链路
的取值会随流程演进而增补，用 `VARCHAR` 列加本文件的 Python 枚举才能做到"新增取值只改代码、
不动表结构"。列宽按 12.5 各字段的取值长度给定（`severity` 取 10、`fail_reason` 取 30、其余
取 20），取值闭集由模型的 `@validates` 调用 `coerce_enum` 在 ORM 层强制。
"""

from enum import Enum
from typing import Any


class EnvSource(str, Enum):
    """环境事实来源。三级兜底的实际命中级别（技术方案 11.1）。

    `sensor_stale` 与 `none` 是两个不同的事实：前者"有传感器但读数过期"仍可给弱提示，
    后者"无任何来源"则该路条目整体被拦截。
    """

    SENSOR = "sensor"
    SENSOR_STALE = "sensor_stale"
    WEATHER = "weather"
    NONE = "none"


class SourceKind(str, Enum):
    """提示条目的输入来源。三值与技术方案 11.2 的三类输入一一对应。"""

    ENV_RISK = "env_risk"
    CALENDAR = "calendar"
    FOLLOWUP = "followup"


class Severity(str, Enum):
    """提示级别档位。五段式展示时必须前置（产品说明书 4.3.3 功能点 2）。"""

    URGENT = "urgent"
    NOTICE = "notice"
    REMINDER = "reminder"


class Channel(str, Enum):
    """触达渠道。`subscribe_once` 为一次性订阅消息，`in_app` 为站内触达。"""

    SUBSCRIBE_ONCE = "subscribe_once"
    IN_APP = "in_app"


class ReachStatus(str, Enum):
    """一次触达的结果。只有两值：成功送达或失败，失败原因另存 `fail_reason`。"""

    DELIVERED = "delivered"
    FAILED = "failed"


class FailReason(str, Enum):
    """触达失败原因四类（技术方案 12.5 / 产品说明书 9.4）。

    `quiet_hours` 与前两者不同质：它是"因免打扰时段被延迟"（可重试），不是真实失败，
    因此必须能与 `no_grant`、`quota_exhausted`、`api_error` 区分开。
    """

    NO_GRANT = "no_grant"
    QUOTA_EXHAUSTED = "quota_exhausted"
    API_ERROR = "api_error"
    QUIET_HOURS = "quiet_hours"


def coerce_enum(enum_cls: type[Enum], value: Any) -> Any:
    """把字符串/枚举成员收敛为枚举的字符串取值；非法值抛 `ValueError`。

    这是"枚举封闭"（全局约定第四节）在 ORM 层的落点：数据库列是 `String`，取值合法性
    必须在写入前拦下，否则自由字符串会污染整条契约链路。`None` 原样返回，以支持可空列。
    """
    if value is None:
        return None
    try:
        return enum_cls(value).value
    except ValueError as exc:
        raise ValueError(f"{value!r} 不是 {enum_cls.__name__} 的合法取值") from exc


__all__ = [
    "Channel",
    "EnvSource",
    "FailReason",
    "ReachStatus",
    "Severity",
    "SourceKind",
    "coerce_enum",
]
