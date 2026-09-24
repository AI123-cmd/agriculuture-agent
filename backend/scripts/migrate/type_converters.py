"""MySQL → PostgreSQL 的三类数据差异转换函数。

技术方案 12.8 列出三类差异：JSON 字段类型、布尔值表示、时间戳时区。本模块只放
纯函数——不连库、不读配置、不写文件，导入即用（T1-14 验收要求"可导入且无副作用"）。

三类差异的共同原则是**报错而不是宽松处理**。静默把无法解析的值写成 NULL 或塞一个
默认值，会让历史数据在 PG 里悄悄失真且事后无法发现；迁移必须可核对。
"""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# 默认源库时区。迁移脚本把它做成命令行参数并要求人工确认：源库若部署在其它
# 时区（或已配置为 UTC），用错时区会让全部时间戳整体平移。
DEFAULT_SOURCE_TZ = "Asia/Shanghai"

# 报错消息里回显的原文长度：够定位到问题行，又不会把一行大 JSON 刷满日志
_MAX_ECHO_CHARS = 200


def convert_json(value: bytes | str | None) -> object | None:
    """MySQL JSON → PG JSONB。PyMySQL 对 JSON 列可能返回 bytes 或 str。

    MySQL 的 `JSON` 列经 PyMySQL 取出来是**字符串**，而 PG 的 `JSONB` 要求传 Python
    对象。直接把字符串塞给 `JSONB`，PG 会当成 JSON 字符串存下来，读出来是
    `"{\\"a\\":1}"` 而不是 `{"a": 1}`——所以必须先 `json.loads`。
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    text = value.strip()
    if text in ("", "null"):
        # 空串与字符串 "null" 都表示"这一列没有值"；JSON 的 null 同样不是有效载荷
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败，原文前 {_MAX_ECHO_CHARS} 字符：{text[:_MAX_ECHO_CHARS]}") from exc


def convert_bool(value: int | bytes | None, *, column: str = "") -> bool | None:
    """MySQL `TINYINT(1)` → PG `boolean`。只接受 0/1/None，其它值报错。

    若某列出现 2，说明它的语义不是布尔（例如 `device.status` 的"故障"），应当改用
    `smallint` 迁移，而不是当成 True 蒙混过去——那样"设备状态永远不是故障"这类问题
    要到线上才被发现。"报错而不是宽松处理"是关键。
    """
    if value is None:
        return None
    ivalue = int(value)
    if ivalue not in (0, 1):
        raise ValueError(f"{column or '该列'} 出现非布尔值 {ivalue}，请改用 smallint 迁移")
    return ivalue == 1


def convert_datetime(value: datetime | str | None, *, source_tz: str = DEFAULT_SOURCE_TZ) -> datetime | None:
    """MySQL naive `datetime` → PG aware `datetime`，按源库时区赋 tz 后转 UTC 存储。

    三类差异里最容易出错的一类。用 `replace(tzinfo=UTC)` 会把北京时间当成 UTC，一条
    `2026-08-30 12:00` 的记录会存成 UTC 12:00 即北京时间 20:00，前端显示整体晚 8 小时、
    "今天"的判断随之出错；反过来完全不处理直接写 `TIMESTAMPTZ`，asyncpg 会按连接时区
    解释，结果依赖 `PGTZ` 环境变量，更不可控。必须显式赋予源库时区再转 UTC。
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(source_tz))
    return value.astimezone(UTC)


__all__ = ["DEFAULT_SOURCE_TZ", "convert_bool", "convert_datetime", "convert_json"]
