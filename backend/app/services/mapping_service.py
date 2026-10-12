"""字段映射保存与三层校验（T5-05，三步接入第三步收尾）。

农户在确认页上点选/改选每个字段的含义后提交**完整映射视图**，本模块校验整份映射的完整性与
自洽性，写入 `field_mapping` 表并返回换算预览。

**校验分三层**，任一层失败都抛 `ValidationError` 并带 `detail={"rawField": ..., "layer": ...}`，
前端据此把错误挂到具体输入框：

- 字段级：`rawField` 非空且 ≤40、必须来自该设备最近一次样例报文、`standardMetric` 落在枚举内；
  单位与 `dataType` 静默覆盖（它们是展示/派生属性，不该因写法差异阻断保存）。
- 映射级：无重复 `rawField`、无重复 `standardMetric`、映射条数 ≤20；P0 缺失只进 `warnings`。
- 换算级：用样例值代入 `value = scale_factor × raw + offset_value`，结果须落在指标合法区间内——
  这是"字段与指标错配"的最后一道拦截（如 `t1=265` 被当成空气温度，试算得 265°C 直接拒绝）。

**校验先于写入**：三层校验是纯规则、不碰数据库，全部通过后才进 `unit_of_work` 执行整体替换，
因此校验失败时不会产生任何部分写入。

**写入策略为整体替换**（`FieldMappingRepository.replace_all`，单事务先删后插）：请求体即最终状态，
前端不需要维护差异，也就不会因漏发删除而留下幽灵映射。删除带 `user_id`，越权 `device_id` 删不掉
他人映射。

**关于"设备置为可接入状态"**：`device_config` 无 `configured` 列，本项不新增列（T5-05 改动范围
表未含迁移），设备的"已配置"由 `field_mapping` 是否存在表达（T5-09 的 `mappedFields` 计数同源）。
"""

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, ValidationError
from app.db.base import new_id
from app.db.uow import unit_of_work
from app.domain.metrics import DEFAULT_UNITS, METRIC_LABELS, METRIC_RANGES, P0_METRICS, StandardMetric
from app.models import FieldMapping
from app.repositories import device_config_repo, field_mapping_repo
from app.schemas.device import MappingItem, MappingPreview, MappingSaveRequest, MappingSaveResponse
from app.services.env_service import parse_payload
from app.services.field_inference import flatten_payload
from app.services.sample_cache import last_sample_payload

logger = logging.getLogger(__name__)

# 单台设备的映射条数上限：防异常报文铺开过多字段。常规多合一墒情站不超过十余个字段。
_MAX_MAPPINGS = 20

# `rawField` 长度上限，与 `field_mapping.raw_field` 的 `String(40)` 对齐。
_MAX_RAW_FIELD_LEN = 40


def _fail(message: str, *, raw_field: str, layer: str) -> ValidationError:
    """构造带字段定位的校验异常。`layer` 让前端知道失败发生在哪一层。"""
    return ValidationError(message, detail={"rawField": raw_field, "layer": layer})


def _infer_data_type(sample_raw: str | None) -> str:
    """从样例值推断 `dataType`（后端覆盖前端回传值）。

    判据刻意保守：能解析成整数 → `int`，能解析成小数 → `float`，其余（`ON`/`OFF`/文本）→ `str`。
    布尔不单独判——环境指标没有布尔语义，把 `ON` 归入 `str` 比给一个无意义的 `bool` 更诚实。
    """
    if sample_raw is None:
        return "float"
    text = sample_raw.strip()
    try:
        number = float(text)
    except ValueError:
        return "str"
    return "int" if number == int(number) else "float"


def _normalize(item: MappingItem) -> FieldMapping:
    """把请求项规整成待写入的映射行：解析指标、覆盖单位与派生字段。

    单位一律取该指标的默认单位：农户或前端可能传 `℃`/`C`/`摄氏度`，它们语义相同，不该因写法
    差异阻断保存。换算参数保留提交值（由其参与换算级试算），但强制转成 float 以落入
    `Numeric(12, 6)`。
    """
    raw_field = item.rawField.strip()
    if not raw_field or len(raw_field) > _MAX_RAW_FIELD_LEN:
        raise _fail("字段名不能为空且不超过 40 个字符", raw_field=raw_field, layer="field")

    try:
        metric = StandardMetric(item.standardMetric)
    except ValueError as exc:
        raise _fail(
            f"指标 {item.standardMetric} 不在支持的范围内，请重新选择", raw_field=raw_field, layer="field"
        ) from exc

    return FieldMapping(
        id=new_id("fm"),
        raw_field=raw_field,
        standard_metric=metric.value,
        unit=DEFAULT_UNITS[metric],
        scale_factor=float(item.scaleFactor),
        offset_value=float(item.offsetValue),
        data_type=_infer_data_type(item.sampleRaw),
        sample_raw=item.sampleRaw,
    )


def _validate_field_level(items: Sequence[FieldMapping], *, sample_fields: set[str] | None) -> None:
    """第一层：字段级校验。

    `sample_fields` 为该设备最近一次样例报文的扁平键集合；为 `None`（缓存未命中或进程重启）时
    跳过"字段必须来自报文"这一条，退化为弱校验而不是拒绝——样例缓存是短时状态，丢失不该让农户
    无法保存一份本来合法的映射。
    """
    for item in items:
        if sample_fields is not None and item.raw_field not in sample_fields:
            raise _fail(
                f"字段 {item.raw_field} 不在设备的报文中，请核对后重新选择",
                raw_field=item.raw_field,
                layer="field",
            )


def _validate_mapping_level(items: Sequence[FieldMapping]) -> list[str]:
    """第二层：映射级校验，返回非阻断提示（`warnings`）。"""
    if len(items) > _MAX_MAPPINGS:
        raise _fail(f"最多支持 {_MAX_MAPPINGS} 个字段的映射，请减少后重试", raw_field="", layer="mapping")

    seen_raw: set[str] = set()
    for item in items:
        if item.raw_field in seen_raw:
            raise _fail(
                f"字段 {item.raw_field} 重复映射，请每个字段只保留一条", raw_field=item.raw_field, layer="mapping"
            )
        seen_raw.add(item.raw_field)

    seen_metric: dict[str, str] = {}
    for item in items:
        previous = seen_metric.get(item.standard_metric)
        if previous is not None:
            # 同一指标由两个字段映射会让读数产生二义（取哪个值？）。本项选择显式拒绝、让农户决定，
            # 而不是引入隐式的"系统自动选一个"规则。
            raise _fail(
                f"指标 {METRIC_LABELS[StandardMetric(item.standard_metric)]} 被 {previous} 与 {item.raw_field} "
                f"同时映射，请只保留一个字段",
                raw_field=item.raw_field,
                layer="mapping",
            )
        seen_metric[item.standard_metric] = item.raw_field

    return _p0_warnings(items)


def _p0_warnings(items: Sequence[FieldMapping]) -> list[str]:
    """P0 指标齐全度提示：缺失 P0 只告警不拒绝（传感器是可选增强层）。"""
    mapped = {StandardMetric(item.standard_metric) for item in items}
    missing = [metric for metric in P0_METRICS if metric not in mapped]
    if not missing:
        return []
    names = "、".join(METRIC_LABELS[metric] for metric in missing)
    return [f"未接入的指标：{names}。缺少这些指标时，对应的提醒将无法使用"]


def _validate_scale_level(items: Sequence[FieldMapping]) -> None:
    """第三层：换算级校验——用样例值试算，结果必须落在指标合法区间内。

    样例值无法数值化（如 `ON`）时跳过该条试算而不是拒绝：布尔类字段没有区间概念，跳过只影响这
    一条映射，不牵连其他字段。
    """
    for item in items:
        if item.sample_raw is None:
            continue
        try:
            raw = float(item.sample_raw.strip())
        except ValueError:
            continue
        value = float(item.scale_factor) * raw + float(item.offset_value)
        metric = StandardMetric(item.standard_metric)
        lo, hi = METRIC_RANGES[metric]
        if not (lo <= value <= hi):
            raise _fail(
                f"字段 {item.raw_field} 的换算结果 {value:g} 超出{METRIC_LABELS[metric]}的合理范围 "
                f"（{lo:g} ~ {hi:g}），请检查换算系数",
                raw_field=item.raw_field,
                layer="scale",
            )


def _skipped_fields(sample_fields: set[str] | None, items: Sequence[FieldMapping]) -> list[str]:
    """被忽略的字段 = 样例报文字段集合 − 已保存映射的原始字段集合（供前端提示）。"""
    if not sample_fields:
        return []
    mapped = {item.raw_field for item in items}
    return sorted(sample_fields - mapped)


def _build_preview(sample: dict[str, Any] | None, saved: Sequence[FieldMapping]) -> list[MappingPreview]:
    """用保存后的映射实跑 `parse_payload` 生成预览，保证口径与上报链路一致。"""
    if not sample:
        return []
    flat = flatten_payload(sample)
    preview: list[MappingPreview] = []
    for reading in parse_payload(sample, saved):
        preview.append(
            MappingPreview(
                rawField=reading.raw_field,
                sampleRaw=str(flat.get(reading.raw_field, "")),
                standardMetric=reading.metric,
                value=reading.value,
                unit=reading.unit or "",
            )
        )
    return preview


async def save_mappings(db: AsyncSession, user_id: str, req: MappingSaveRequest) -> MappingSaveResponse:
    """保存一台设备的完整字段映射（三步接入第三步）。

    设备必须属于当前农户：`device_config_repo.get` 读不到他人设备返回 `None`，折算为 404。
    三层校验全部通过后才写入，写入在单个 `unit_of_work` 内做整体替换。
    """
    device = await device_config_repo.get(db, user_id, req.deviceId)
    if device is None:
        raise NotFound("设备不存在或无权访问")

    normalized = [_normalize(item) for item in req.mappings]

    sample = await last_sample_payload(user_id, device.id)
    sample_fields = set(flatten_payload(sample)) if sample else None
    if sample_fields is None:
        logger.info("样例报文缓存未命中，跳过字段来源校验：device=%s", device.id)

    _validate_field_level(normalized, sample_fields=sample_fields)
    warnings = _validate_mapping_level(normalized)
    _validate_scale_level(normalized)

    async with unit_of_work(db, name="device.save_mappings"):
        await field_mapping_repo.replace_all(db, user_id, device.id, normalized)
        # flush 后以库中行为准生成预览：预览必须反映真正落库的映射，而非内存中的请求对象。
        await db.flush()
        saved = list(await field_mapping_repo.list_for_device(db, user_id, device.id))

    logger.info("字段映射保存成功：device=%s user=%s count=%d", device.id, user_id, len(saved))
    return MappingSaveResponse(
        deviceId=device.id,
        saved=len(saved),
        skipped=_skipped_fields(sample_fields, normalized),
        warnings=warnings,
        preview=_build_preview(sample, saved),
    )


__all__ = ["save_mappings"]
