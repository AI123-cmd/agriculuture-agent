"""日志与 trace 的统一脱敏入口（T6-08，技术方案 14.4）。

可观测性与隐私在这里直接冲突：为了让问题可复盘要把上下文写全，但农户手机号、设备原始
标识、精确地理坐标都属于个人与生产信息。"为了可观测把隐私写进了日志"是这类系统最常见
的合规问题，因此脱敏必须是**统一入口**而不是各处的自觉——靠"日志与 trace 只经由一个
序列化器落盘"这条工程约束成立（见 `logging.py` 的处理器链）。

### 两类处理，边界不同

- **手机号**：正则模式足够具体（`1[3-9]` 开头的 11 位），故对**所有字符串值**全局清洗，
  包括异常堆栈文本。完全掩成 `***` 会让"同一农户的两条日志无法关联"，保留号段与尾 4 位
  （`138****0000`）既便于排查又不构成可识别信息。
- **设备 ID**：模式较宽（6 位前缀 + 6 位以上），**只对声明的敏感字段**清洗。全局套用会把
  日志里的长十六进制摘要（如审计哈希）误伤成 `AB12CD******`，那是比漏掩更糟的回归。

### 递归与"整体替换"

日志字段可能嵌套（如 `prescription={"facts_provenance": {"phone": ...}}`）。`mask` 递归进
嵌套 dict 与 list；但**命中敏感字段名时整体替换**（非字符串直接给 `***`），不再下钻——
把 `raw_payload` 这类"整块都敏感"的字段逐键清洗，反而会因键名不匹配而漏掉内容。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# 手机号：保留号段与前 3 位、掩掉中间 4 位与末 4 位。用后顾/前瞻断言避免匹配到更长数字串
# 的子串（如银行卡号里恰好出现的一段）。
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
# 设备序列号：恰好 6 位不算（那可能是普通短标识），要求 6 位前缀后仍跟 6 位以上。
DEVICE_SN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{6})[A-Za-z0-9\-]{6,}(?![A-Za-z0-9])")

# 地理坐标字段。是否脱敏由 `mask_geo` 决定（本地调试需要真实坐标，生产必须开启脱敏）。
GEO_FIELDS: tuple[str, ...] = ("latitude", "longitude")

# 默认按名整体脱敏的字段。前四项是个人/联系方式，`device_sn`/`device_id` 可反查农户归属，
# `raw_payload` 是设备原始报文（可能含农户自定义字段）。坐标另由 GEO_FIELDS 处理。
DEFAULT_FIELDS: tuple[str, ...] = (
    "phone",
    "phone_number",
    "mobile",
    "real_name",
    "address",
    "device_sn",
    "device_id",
    "raw_payload",
)

_MASK = "***"
_DEVICE_SUFFIX = "*" * 6


class ObservabilityMasker:
    """日志与 trace 的统一脱敏入口。所有落盘内容必须经 `mask()`。"""

    def __init__(self, extra_fields: tuple[str, ...] = (), *, mask_geo: bool = False) -> None:
        self._fields = frozenset(DEFAULT_FIELDS) | frozenset(extra_fields)
        self._geo = frozenset(GEO_FIELDS)
        self._mask_geo = mask_geo
        # 命中即"整体替换"的字段集合（坐标只在 mask_geo 开启时算在内）。
        self._sensitive = self._fields | (self._geo if mask_geo else frozenset())

    def mask_phone(self, value: str) -> str:
        """`138****0000`：保留号段与前 3 位便于识别，掩掉中间与末 4 位。"""
        return PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)

    def mask_device(self, value: str) -> str:
        """保留前 6 位便于定位批次，其余替换为固定长度的 `*`。"""
        return DEVICE_SN_RE.sub(lambda m: f"{m.group(1)}{_DEVICE_SUFFIX}", value)

    def mask_value(self, key: str, value: Any) -> Any:
        """按字段名处理单个标量值。坐标与敏感字段整体替换为 `***`。"""
        if key in self._geo:
            # 坐标始终整体替换：无论是浮点还是字符串，逐字符清洗都可能留下数值。
            return value if not self._mask_geo else _MASK
        if key in self._fields:
            if isinstance(value, str):
                return self.mask_phone(self.mask_device(value))
            return _MASK
        # 非敏感字段：仅全局清洗手机号（模式具体，不会误伤普通长字符串）。
        return self.mask_phone(value) if isinstance(value, str) else value

    def mask(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """递归脱敏，返回新字典（不改动入参）。"""
        return {str(key): self._walk(str(key), value) for key, value in event_dict.items()}

    def _walk(self, key: str, value: Any) -> Any:
        if key in self._sensitive:
            return self.mask_value(key, value)
        if isinstance(value, Mapping):
            return {str(k): self._walk(str(k), v) for k, v in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [self._walk(key, item) for item in value]
        return self.mask_value(key, value)


__all__ = ["DEFAULT_FIELDS", "DEVICE_SN_RE", "GEO_FIELDS", "PHONE_RE", "ObservabilityMasker"]
