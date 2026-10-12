"""环境读数解析、质量标记与上报（T5-05 预览 / T5-06 上报 / T5-07 质量标记）。

`parse_payload` 是 T5-05 与 T5-06 共享的**纯函数**：T5-05 用它做保存后的读数预览，T5-06
用它做上报落库解析。两处共用同一实现，保证"预览对、上报也对"——若各写一份，预览的换算口径
与落库口径迟早分叉，而分叉不会报错，只会让农户看到的预览和实际入库的值悄悄不同。

三条边界约定（T5-06 文档第六节，T5-05 与 T5-06 一致）：

- 只取 mappings 中出现的 `raw_field`，报文里多余的字段忽略（设备固件升级可能多发字段，
  不应导致解析失败）；
- 缺字段或无法数值化的映射项跳过，不产出行（一个字段的缺失不拖垮整批读数）；
- `observed_at` 取报文中的时间戳，缺失或非法则用服务端当前时间，并在读数上记录
  `timestamp_source`（`device` / `server`），供 T5-07 区分"设备报的时间"与"平台收到的时间"。

`classify_quality`（T5-07）在写库前给每条读数打质量标记：范围合格性与时效都不合格时，范围问题
优先（`suspect` 优先于 `stale`）。窗口常量取自 `app/domain/metrics.py`，与 T3-02 环境事实采集
同源，避免两处判断悄悄分叉。

`ingest_report`（T5-06）是设备上报的编排入口：密钥 → 设备存在 → 归属 → 字段映射 → 数值范围，
**除 403 外任何失败都返回 200 + `note`**，避免设备固件对 5xx 重试形成压力风暴。
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.device_secret import verify_device_secret
from app.core.exceptions import DeviceAuthError
from app.db.uow import unit_of_work
from app.domain.metrics import METRIC_RANGES, REFERENCE_WINDOW_HOURS, StandardMetric
from app.models import EnvReading, FieldMapping
from app.repositories import device_config_repo, env_reading_repo, field_mapping_repo
from app.services.field_inference import flatten_payload, load_payload

logger = logging.getLogger(__name__)

# 报文时间戳的候选叶子名。取"第一个能解析成时间的"，避免因不同厂商的键名差异而把有时间的报文
# 当无时间处理。`ts`/`timestamp` 覆盖多数国产设备，`@timestamp` 覆盖 ELK 系；匹配在扁平化后的
# 叶子名上进行，因此 `data.ts` 也能命中。
_TIMESTAMP_LEAVES: tuple[str, ...] = ("ts", "timestamp", "time", "@timestamp", "observed_at", "report_time")


@dataclass
class Reading:
    """单条标准读数（一个原始字段 → 一个指标）。

    与 `app/tools/sensors.py::Reading`（一批读数）区分：那是"同一时刻的一组指标"，用于读取侧
    的新鲜度判定；本类是**落库单位**，一行对应 `env_reading` 表的一行。
    """

    metric: str
    value: float
    raw_field: str
    unit: str | None
    observed_at: datetime
    timestamp_source: str
    quality: str = field(default="ok")


def _as_float(value: Any) -> float | None:
    """尽力数值化；布尔与不可数值化文本返回 `None`。

    布尔刻意排除：`true`/`false` 数值化后是 1/0，落在多数指标量程内会被当成"合法读数"，
    而它实际是开关量，不该作为环境值落库。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    """把报文时间戳解析成带时区的 `datetime`；无法解析返回 `None`。

    支持两种形态：数值/数字串按 Unix 秒处理，字符串按 ISO 8601 处理（容忍尾随 `Z`）。
    毫秒级时间戳（13 位）也接受：基层设备固件常发毫秒，直接按秒解析会得到公元 5 万年。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().rstrip("Z")
        if not text:
            return None
        try:
            seconds = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    # 13 位及以上视为毫秒
    if abs(seconds) >= 1e11:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_timestamp(flat: dict[str, Any]) -> tuple[datetime, str]:
    """从扁平化报文里取时间戳；缺失或非法时回退服务端当前时间并标记来源为 `server`。

    匹配叶子名（最后一段）而非全路径：`data.ts` 与顶层 `ts` 是同一语义，不该因嵌套而漏取。
    """
    for path, value in flat.items():
        if path.rsplit(".", 1)[-1] in _TIMESTAMP_LEAVES:
            parsed = _parse_timestamp(value)
            if parsed is not None:
                return parsed, "device"
    return datetime.now(UTC), "server"


def parse_payload(payload: dict[str, Any], mappings: Sequence[FieldMapping]) -> list[Reading]:
    """按字段映射把报文翻译成标准读数列表。

    只处理 `mappings` 中出现的原始字段（缺字段跳过、多余字段忽略），每个映射项最多产出一行；
    数值换算为 `value = scale_factor * raw + offset_value`。

    取值按扁平化后的点分路径匹配：`raw_field` 存的是 `data.air_temp` 这类路径（与模板
    `field_spec` 同口径），因此先把报文扁平化再逐项取，嵌套报文才能被正确解析。
    """
    flat = flatten_payload(payload)
    observed_at, timestamp_source = _extract_timestamp(flat)
    readings: list[Reading] = []
    for mapping in mappings:
        number = _as_float(flat.get(mapping.raw_field))
        if number is None:
            continue
        value = float(mapping.scale_factor) * number + float(mapping.offset_value)
        readings.append(
            Reading(
                metric=mapping.standard_metric,
                value=value,
                raw_field=mapping.raw_field,
                unit=mapping.unit,
                observed_at=observed_at,
                timestamp_source=timestamp_source,
            )
        )
    return readings


# 超前容忍度：设备时钟超前超过它即视为不可信时间戳（标 `suspect` 而非 `stale`——它不是"旧"，
# 是"不可信"）。基层设备的 RTC 电池耗尽后跳到出厂时间很常见，不处理会让 `latest()` 取到一条
# 未来时间戳的读数并当作最新。
CLOCK_SKEW_TOLERANCE = timedelta(hours=1)


def in_valid_range(metric: str, value: float) -> bool:
    """量程判定：闭区间 `lo <= value <= hi`。

    端点值算合法：`soil_moisture=0` 在极端干旱时是实测可能值，不该被标 `suspect`。判定阈值与
    "合法物理范围"对齐而非与"常见值范围"对齐。区间取自 `StandardMetric`，与 T5-05 换算级校验
    同源，避免"保存时判合法、落库时判非法"这种自相矛盾。
    """
    lo, hi = METRIC_RANGES[StandardMetric(metric)]
    return lo <= value <= hi


def _stale(observed_at: datetime | None, now: datetime) -> bool:
    """超出参考窗口（24 小时）即 `stale`；`observed_at` 缺失视为 `stale`（无法证明新鲜）。"""
    if observed_at is None:
        return True
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return (now - observed_at) > timedelta(hours=REFERENCE_WINDOW_HOURS)


def _skewed(observed_at: datetime | None, now: datetime) -> bool:
    """设备时钟超前超过容忍度，时间戳不可信。"""
    if observed_at is None:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at - now > CLOCK_SKEW_TOLERANCE


def classify_quality(reading: Reading, *, now: datetime) -> str:
    """三值判定：`ok` / `suspect` / `stale`。

    优先级：范围问题 → 时钟超前 → 时效。
    - 数值超范围标 `suspect`（保留原始值供排查，不参与阈值判断）；
    - 时间戳超前超容忍度也标 `suspect`（不可信）；
    - 超参考窗口标 `stale`（仅看板参考展示）。

    服务端补的时间戳（`timestamp_source == "server"`）不做时效判定：它就是当前时间，判 `stale`
    只会把"设备不报时间、平台补当前时间"的正常情况误伤。超前判定同理只对设备时间戳生效。
    """
    if not in_valid_range(reading.metric, reading.value):
        return "suspect"
    if reading.timestamp_source == "device" and _skewed(reading.observed_at, now):
        return "suspect"
    if _stale(reading.observed_at, now):
        return "stale"
    return "ok"


def _to_env_readings(user_id: str, device_id: str, farm_entry_id: str | None, readings: Sequence[Reading]) -> list[EnvReading]:
    """把读数对象转成待落库的 `env_reading` 行。

    `source` 固定 `sensor`：本入口的读数一律来自设备直报（天气与静态兜底不走此路径）。
    """
    return [
        EnvReading(
            user_id=user_id,
            device_id=device_id,
            farm_entry_id=farm_entry_id,
            metric=reading.metric,
            value=reading.value,
            unit=reading.unit,
            source="sensor",
            observed_at=reading.observed_at,
            quality=reading.quality,
        )
        for reading in readings
    ]


async def ingest_report(db: AsyncSession, device_sn: str, presented_secret: str, raw_payload: str) -> dict[str, Any]:
    """设备 HTTP 直报入口的编排（T5-06）。

    校验顺序（不可调换，先廉价后昂贵、先安全后业务）：密钥 → 设备存在 → 归属（未解绑）→ 字段映射
    → 报文解析 → 数值范围。除密钥/设备失败返回 403 外，其余失败一律 200 + `note`，避免设备重试
    风暴。

    `raw_payload` 收原始文本而非 `dict`：报文可能不是合法 JSON（简易固件发错格式），此时要返回
    200 `payload_unparsable` 而不是让 FastAPI 的 schema 校验抛 422——后者会被固件当成可重试错误。
    解析容错（全角引号、尾随逗号）复用 `load_payload`，与 T5-04 粘贴解析同一口径。

    `note` 取值：`payload_unparsable`（报文不可用）/ `no_mapping`（无映射字段命中）/
    `mapping_missing`（设备未配置映射）/ `deferred`（落库瞬时故障）；正常路径 `note` 为 `None`。
    """
    device = await device_config_repo.get_by_sn(db, device_sn)
    # 密钥校验与设备存在合并返回同一 403，使攻击者无法靠响应差异枚举合法 SN。
    if device is None or not verify_device_secret(device, presented_secret):
        logger.warning("设备上报鉴权失败：sn=%s", device_sn)
        raise DeviceAuthError()

    mappings = list(await field_mapping_repo.list_for_device(db, device.user_id, device.id))
    if not mappings:
        logger.info("设备未配置字段映射，上报被忽略：sn=%s", device_sn)
        return {"accepted": 0, "flagged": 0, "note": "mapping_missing"}

    payload = load_payload(raw_payload)
    if payload is None:
        logger.info("设备上报报文不可解析：sn=%s", device_sn)
        return {"accepted": 0, "flagged": 0, "note": "payload_unparsable"}

    readings = parse_payload(payload, mappings)
    if not readings:
        logger.info("报文没有命中任何映射字段：sn=%s", device_sn)
        return {"accepted": 0, "flagged": 0, "note": "no_mapping"}

    now = datetime.now(UTC)
    accepted: list[Reading] = []
    flagged: list[Reading] = []
    for reading in readings:
        reading.quality = classify_quality(reading, now=now)
        (accepted if reading.quality == "ok" else flagged).append(reading)

    rows = _to_env_readings(device.user_id, device.id, device.farm_entry_id, accepted + flagged)
    try:
        async with unit_of_work(db, name="device.ingest_report"):
            await env_reading_repo.bulk_insert(db, rows)
            await device_config_repo.touch_last_report(db, device.user_id, device.id, reported_at=now, online="online")
    except Exception:  # noqa: BLE001  落库瞬时故障不让设备重试，记日志由运维排查
        logger.exception("设备上报落库失败，已放弃本批读数：sn=%s", device_sn)
        return {"accepted": 0, "flagged": 0, "note": "deferred"}

    logger.info("设备上报成功：sn=%s accepted=%d flagged=%d", device_sn, len(accepted), len(flagged))
    return {"accepted": len(accepted), "flagged": len(flagged), "note": None}


__all__ = ["CLOCK_SKEW_TOLERANCE", "Reading", "classify_quality", "in_valid_range", "ingest_report", "parse_payload"]
