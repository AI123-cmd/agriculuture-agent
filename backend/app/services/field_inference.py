"""字段推断：报文扁平化与两层启发式（T5-04，技术方案 13.4）。

三步接入的第二步是"傻瓜化"的核心：农户粘贴一条设备报文，系统解析出所有字段并推断每个字段代表
哪个标准环境指标，农户只需确认或改选。推断全为纯规则，不引入模型调用——字段对应关系用规则即可
判定，这符合全局约定"凡是能被规则判定的，不交给模型"，规则的确定性更高、延迟更低、可单测。

**两层启发式的取舍**：字段名（`t1`/`temp` → 温度）优先于数值范围。字段名表达的是设备厂商的意图，
数值范围只是旁证——`78` 既可能是空气湿度也可能是土壤水分，只有字段名能区分。因此：

- 字段名命中时给 `inferredMetric`，并用数值范围结论抬升置信度；
- 只有数值范围有结论时不设 `inferredMetric`，只把范围允许的指标放进 `candidates`。

第二条是刻意的保守：文档明确"给它们一个错误的推断比留空更糟，因为农户会照着点确认"。`battery=3.7`
这类设备状态字段没有字段名线索，数值范围又落在多个指标的交集里，默认填一个只会诱导错配。

`candidates` 的约定：`inferredMetric`（非空时）排第一位，其后是范围旁证指标，最后一项固定为
`IGNORE_OPTION`（"忽略此字段"）——它不属于 `StandardMetric`，保存时该字段被跳过。
"""

import json
import re
from typing import Any

from app.domain.metrics import StandardMetric
from app.schemas.device import DetectedField

# "忽略此字段"选项：设备状态类字段（电池、信号强度）不对应任何环境指标，农户选它即跳过。
IGNORE_OPTION = "ignore"

# 字段名规则：正则 → 指标 → 置信度。匹配在字段路径的**最后一段**上进行（`data.air_temp` 取
# `air_temp`），这样阿里云/腾讯云等嵌套报文的 `params.air_temp` 也能命中。规则刻意宽松，把
# 常见缩写都列上，避免农户粘贴的报文因字段名写法不同而得不到推断。
NAME_RULES: list[tuple[re.Pattern[str], StandardMetric, float]] = [
    (re.compile(r"^(t|temp|tem|t1|t2|airt|air_temp)$", re.I), StandardMetric.AIR_TEMP, 0.7),
    (re.compile(r"^(h|hum|humi|h1|h2|rh|air_humi|air_humidity)$", re.I), StandardMetric.AIR_HUMIDITY, 0.7),
    (re.compile(r"^(st|soilt|soil_temp|tt)$", re.I), StandardMetric.SOIL_TEMP, 0.75),
    (re.compile(r"^(sm|soil|soil_moisture|vwc|wc|soil_humi)$", re.I), StandardMetric.SOIL_MOISTURE, 0.75),
    (re.compile(r"^(rain|rf|rainfall|precip)$", re.I), StandardMetric.RAINFALL, 0.7),
    (re.compile(r"^(lux|light|lux1|illuminance)$", re.I), StandardMetric.LIGHT_LUX, 0.7),
    (re.compile(r"^(ws|wind|wind_speed)$", re.I), StandardMetric.WIND_SPEED, 0.6),
    (re.compile(r"^(ph|soil_ph)$", re.I), StandardMetric.SOIL_PH, 0.7),
    (re.compile(r"^(ec|soil_ec)$", re.I), StandardMetric.SOIL_EC, 0.7),
]

# 形似布尔/开关的取值：0/1、true/false、ON/OFF。这类字段不推断（可能是开关量而非环境读数）。
_BOOL_TEXT = {"ON", "OFF", "TRUE", "FALSE"}

# 置信度上限：两层结论一致时相加，但不超过它——留出"仍需农户确认"的空间，不宣称绝对确定。
_CONFIDENCE_CAP = 0.95


def _strip_dirty(text: str) -> str:
    """清洗粘贴文本中的常见脏字符：BOM、全角引号、尾随逗号。

    农户从聊天窗口或设备说明书复制时，实际形态常带全角引号与尾随逗号；不做清洗会让"明明是正确
    报文"的粘贴失败。清洗只在解析层做，产出的标准 JSON 才进 `sample_raw`。
    """
    cleaned = text.lstrip("\ufeff").strip()
    for full, half in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
        cleaned = cleaned.replace(full, half)
    # 尾随逗号（`{"a":1,}`）不是合法 JSON，去掉对象/数组闭合前的逗号。
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    return cleaned


def load_payload(text: str) -> dict[str, Any] | None:
    """容错解析粘贴的报文本，返回字典；不可解析或顶层不是对象时返回 `None`。

    只接受顶层对象：数组、标量不是设备报文形态，交给调用方给友好提示，而不是硬凑成字段表。
    """
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(_strip_dirty(text))
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def flatten_payload(payload: Any, prefix: str = "") -> dict[str, Any]:
    """把报文扁平化成 `点分路径 → 叶子值`，与模板 `field_spec` 的键口径一致。

    嵌套结构与数组都展开：阿里云/腾讯云的 `params.air_temp`、OneNET 的 `dp.soil_temp.0.v` 都是
    点分键。数组用下标入路径，使同一路径在不同报文里稳定可复现。
    """
    out: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten_payload(value, path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            out.update(flatten_payload(item, f"{prefix}.{index}"))
    else:
        out[prefix] = payload
    return out


def _leaf_name(raw_field: str) -> str:
    """取点分路径的最后一段，作为字段名启发式的匹配对象。"""
    return raw_field.rsplit(".", 1)[-1]


def _is_boolish(value: Any) -> bool:
    """形似开关量：布尔、`ON`/`OFF` 文本、或 0/1 数值。"""
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip().upper() in _BOOL_TEXT
    if isinstance(value, (int, float)):
        return value in (0, 1)
    return False


def _as_float(value: Any) -> float | None:
    """尽力把取值数值化；布尔与不可数值化的文本返回 `None`。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _match_by_name(raw_field: str) -> tuple[StandardMetric, float] | None:
    """字段名启发式：命中规则返回 (指标, 置信度)，否则 `None`。"""
    name = _leaf_name(raw_field)
    for pattern, metric, confidence in NAME_RULES:
        if pattern.fullmatch(name):
            return metric, confidence
    return None


def match_by_range(value: Any) -> tuple[list[StandardMetric], float]:
    """数值范围启发式：返回 (候选指标列表, 置信度)。

    判据取自 `METRIC_RANGES` 同一处定义，避免"推断用一套范围、校验用另一套"。只保留三档互相不冲突
    的形态：

    - 小数且落在 `soil_ph` 量程（0~14）内 → pH；
    - 小数且落在温度量程（0~50）内 → 温度（空气或土壤二选一，交农户点选）；
    - 整数且落在湿度/水分量程（0~100）内 → 湿度或土壤水分。

    刻意**不做**范围级的土壤 EC 推断：`soil_ec` 量程（0~20）与温度量程（0~50）大面积重叠，14~20
    这一段的常见气温会被判成 EC，误判代价高于收益。EC 只靠字段名线索（`ec` / `soil_ec`）识别，
    没有字段名线索时留空、交农户点选，符合"错误的默认值比留空更糟"的取向。

    温度/湿度两档给两个候选而非单一默认值同理：`78` 既可能是空气湿度也可能是土壤水分，只有字段名
    能区分；给一个错的默认值会诱导农户直接点确认，比让他二选一更糟。
    """
    if _is_boolish(value):
        return [], 0.2
    number = _as_float(value)
    if number is None:
        return [], 0.0
    fractional = number != int(number)
    if fractional and 0 <= number <= 14:
        return [StandardMetric.SOIL_PH], 0.5
    if fractional and 0 <= number <= 50:
        return [StandardMetric.AIR_TEMP, StandardMetric.SOIL_TEMP], 0.5
    if not fractional and 0 <= number <= 100:
        return [StandardMetric.AIR_HUMIDITY, StandardMetric.SOIL_MOISTURE], 0.5
    return [], 0.0


def _infer_one(raw_field: str, raw_value: Any) -> DetectedField:
    """推断单个字段：合并字段名与数值范围两层结论，产出契约对象。"""
    name_hint = _match_by_name(raw_field)
    range_metrics, range_conf = match_by_range(raw_value)

    if name_hint is not None:
        metric, confidence = name_hint
        if metric in range_metrics:
            confidence = min(confidence + range_conf, _CONFIDENCE_CAP)
        candidates = [metric.value] + [m.value for m in range_metrics if m != metric]
    else:
        # 仅范围有结论：不设 inferredMetric（错误的默认值比留空更糟），只给候选供农户点选。
        metric, confidence = None, range_conf
        candidates = [m.value for m in range_metrics]

    return DetectedField(
        rawField=raw_field,
        sampleValue=str(raw_value),
        inferredMetric=metric.value if metric is not None else None,
        candidates=_dedup_candidates(candidates),
        confidence=confidence,
    )


def _dedup_candidates(values: list[str]) -> list[str]:
    """候选去重并追加"忽略此字段"选项，使它可以被农户点选（排在末位）。"""
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    if IGNORE_OPTION not in out:
        out.append(IGNORE_OPTION)
    return out


def infer_fields(flat: dict[str, Any]) -> list[DetectedField]:
    """对扁平化后的报文字段逐一推断，保持报文字段的自然顺序。"""
    return [_infer_one(raw_field, raw_value) for raw_field, raw_value in flat.items()]


def template_covers(field_spec: dict[str, Any], flat: dict[str, Any]) -> bool:
    """模板 `field_spec` 的键集合是否为报文扁平键集合的子集。

    不是子集即视为"模板与实际报文不符"（同一 SN 换了固件，或农户粘贴了另一台设备的报文），
    调用方退回启发式而非强行套用模板。
    """
    return bool(field_spec) and set(field_spec).issubset(flat.keys())


def fields_from_template(field_spec: dict[str, Any], flat: dict[str, Any]) -> list[DetectedField]:
    """模板命中时直接采用模板的预映射。

    `confidence` 固定 1.0：模板是人工核实过的对应关系，不是猜测。`sampleValue` 取报文中的实际
    值（而非模板里的 `sample_value`），使农户能对着自己的设备数据核对。
    """
    fields: list[DetectedField] = []
    for raw_field, spec in field_spec.items():
        metric = spec.get("standard_metric")
        fields.append(
            DetectedField(
                rawField=raw_field,
                sampleValue=str(flat.get(raw_field, spec.get("sample_value", ""))),
                inferredMetric=str(metric) if metric else None,
                candidates=_dedup_candidates([str(metric)] if metric else []),
                confidence=1.0,
            )
        )
    return fields


def any_metric_inferred(fields: list[DetectedField]) -> bool:
    """是否至少有一个字段被推断出指标；全为设备状态字段时调用方据此给友好提示。"""
    return any(field.inferredMetric is not None for field in fields)


__all__ = [
    "IGNORE_OPTION",
    "NAME_RULES",
    "any_metric_inferred",
    "fields_from_template",
    "flatten_payload",
    "infer_fields",
    "load_payload",
    "match_by_range",
    "template_covers",
]
