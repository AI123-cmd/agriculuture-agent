"""设备接入服务：绑定设备、样例报文解析与字段推断（T5-03 / T5-04，技术方案 13.4）。

三步接入的第 1、2 步都在本模块：绑定设备编号并下发密钥，以及解析农户粘贴的样例报文、对每个字段
推断它代表哪个标准环境指标。两步共用"归属校验走仓储"的规矩。

**归属校验一律走仓储**（守全局约定 L4，服务层不出现裸 ORM 查询）：`user_id` 只从令牌取
（路由层 `get_current_user`），不接受请求体传入（产品说明书 4.5.2 功能点 4 的数据隔离）。

**重复绑定分三种情况**（产品说明书 7.4）：

- 同一农户重复绑定同一 SN：幂等返回既有 `deviceId`，**不重发密钥**。农户重复点击时若密钥被刷新，
  之前已配进设备的密钥会立即失效，表现为"设备突然不上报了"，这是幂等要覆盖的核心场景。
- 设备已绑定到他人：409。防止数据串号。
- 设备曾被解绑：复用原行（`unbound_at` 置空、重置密钥与绑定时刻、允许换主人），不新增一条撞唯一
  约束的行。并发抢绑由 `rebind` 的 `unbound_at IS NOT NULL` 条件作乐观锁，后到者受影响行数为 0，
  折算为 409。详见 `DeviceConfigRepository.rebind`。

**密钥只在下发时出现一次**：落库的是引用名与加盐摘要，明文不落任何表（`app/core/device_secret.py`）。
"""

import logging
import re
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.device_secret import generate_device_secret, hash_device_secret
from app.core.exceptions import ConflictError, NotFound, ValidationError
from app.db.uow import unit_of_work
from app.models import DeviceConfig
from app.repositories import device_config_repo, vendor_template_repo
from app.schemas.device import BindRequest, BindResponse, ParseSampleRequest, ParseSampleResponse
from app.services.field_inference import (
    any_metric_inferred,
    fields_from_template,
    flatten_payload,
    infer_fields,
    load_payload,
    template_covers,
)

logger = logging.getLogger(__name__)

# 设备编号允许的字符集：字母、数字与常见分隔符。刻意放宽（不同厂商编号规则差异大），
# 只排除空白与控制字符——把合法编号误判为非法比放过一个怪编号的代价更高。
_SN_ALLOWED = re.compile(r"^[A-Za-z0-9._:\-]+$")

# 设备编号长度上限与库列 `String(60)` 一致（schema 已拦，这里兜底）。
_SN_MAX_LEN = 60

# 绑定协议：三档接入的 L1 HTTP 直报（调研报告 8.2 推荐起点）。
_PROTOCOL_HTTP_DIRECT = "http_direct"


def _valid_sn_format(sn: str) -> bool:
    """设备编号格式校验：只做三条——非空、长度不超上限、不含空白与非法字符。

    不做前缀正则强约束：编号规则因厂商而异（`HN` 开头只是本项目示例约定），用强正则会把
    合法编号误判为非法，代价高于放过个别怪编号。格式校验必须在绑定时就做，不能等到上报
    （T5-06）才发现编号不合法。
    """
    if not sn or len(sn) > _SN_MAX_LEN:
        return False
    return _SN_ALLOWED.fullmatch(sn) is not None


def _model_from_sn(sn: str) -> str | None:
    """从设备编号推导型号提示。

    扫码带出的编号里常含型号（如 `Ali-4G-01`），直接作为 `model_hint` 交给模板匹配的型号层；
    不含型号的编号（如 `HN123`）匹配不到非通用模板，自然落到通用兜底。返回 `None` 时模板匹配
    只看报文结构。
    """
    return sn or None


def _report_url(sn: str) -> str:
    """设备上报地址：`DEVICE_REPORT_BASE_URL` + `/api/device/report/{sn}`。

    用配置项而非请求 Host 头推导：反向代理后 Host 头是内网地址，设备拿到后无法回连；且产品
    说明书 7.4 要求域名必须预先备案。基址为空时返回相对路径（开发环境便于本地拼接）。
    """
    base = get_settings().DEVICE_REPORT_BASE_URL.rstrip("/")
    return f"{base}/api/device/report/{sn}"


def _bind_response(device: DeviceConfig, *, matched: bool, secret: str | None) -> BindResponse:
    """组装绑定回执：下一步引导 + 一次性的密钥与上报地址下发。

    `templateId` 取设备上记录的模板引用；`nextStep` 命中模板为 `confirm_template`，否则
    `provide_sample`。幂等分支 `secret` 传 `None`，前端据此不刷新已配进设备的密钥。
    """
    template_id = device.vendor_template_id
    return BindResponse(
        deviceId=device.id,
        vendorTemplateMatched=matched,
        templateId=template_id if matched else None,
        nextStep="confirm_template" if matched else "provide_sample",
        reportUrl=_report_url(device.device_sn),
        deviceSecret=secret,
    )


async def bind_device(db: AsyncSession, user_id: str, req: BindRequest) -> BindResponse:
    """三步接入第一步：绑定设备编号并下发设备密钥。

    SN 的格式校验放在最前：编号不合法时不必查询设备，也不该生成密钥。
    """
    normalized = req.deviceSn.strip()
    farm_entry_id = req.farmEntryId
    if not _valid_sn_format(normalized):
        raise ValidationError("设备编号格式不正确，请核对后重新输入")

    async with unit_of_work(db, name="device.bind"):
        existing = await device_config_repo.get_by_sn_any(db, normalized)

        if existing is not None and existing.unbound_at is None:
            if existing.user_id == user_id:
                # 已绑定在同一农户名下：幂等返回，不重复下发密钥
                return _bind_response(existing, matched=existing.vendor_template_id is not None, secret=None)
            raise ConflictError("该设备已被绑定，请核对设备编号或联系客服")

        secret = generate_device_secret()
        secret_ref, digest = hash_device_secret(secret)

        if existing is not None:
            # 曾被解绑：复用同一行，重置绑定关系与密钥（归属不符时受影响行数为 0，返回 None）
            device = await device_config_repo.rebind(
                db,
                user_id,
                existing.id,
                farm_entry_id=farm_entry_id,
                secret_ref=secret_ref,
                secret_digest=digest,
                bound_at=datetime.now(UTC),
            )
            if device is None:
                raise ConflictError("该设备已被绑定，请核对设备编号或联系客服")
        else:
            device = await device_config_repo.create(
                db,
                user_id,
                device_sn=normalized,
                farm_entry_id=farm_entry_id,
                protocol_type=_PROTOCOL_HTTP_DIRECT,
                secret_ref=secret_ref,
                secret_digest=digest,
            )

        matched_tpl = await vendor_template_repo.match(db, {}, model_hint=_model_from_sn(normalized))
        if matched_tpl is not None:
            await device_config_repo.update_fields(db, user_id, device.id, vendor_template_id=matched_tpl.id)
            device.vendor_template_id = matched_tpl.id

        logger.info("设备绑定成功：sn=%s user=%s template=%s", normalized, user_id, matched_tpl and matched_tpl.id)
        return _bind_response(device, matched=matched_tpl is not None, secret=secret)


async def parse_sample(db: AsyncSession, user_id: str, req: ParseSampleRequest) -> ParseSampleResponse:
    """解析农户粘贴的样例报文并对字段做两层推断（三步接入第二步）。

    设备必须已绑定在本农户名下：`device_config_repo.get` 读不到他人设备返回 `None`（不区分"不
    存在"与"不属于你"），此处折算为 404。

    模板优先：设备绑定时命中的模板若覆盖了这条报文的字段，直接采用模板的预映射（`confidence=1.0`）；
    模板不覆盖（键集合不是报文键的子集，说明固件改过或农户粘错报文）则退回启发式。

    失败一律返回 200 + `parsed=false` + 友好提示，不抛 5xx：这是农户在配置页上的交互入口，错误应
    以文案形式呈现给他，而不是页面报错。
    """
    device = await device_config_repo.get(db, user_id, req.deviceId)
    if device is None:
        raise NotFound("设备不存在或无权访问")

    payload = load_payload(req.payload)
    if payload is None:
        return ParseSampleResponse(parsed=False, fields=[], inferError="报文格式不正确，请粘贴一条完整的 JSON 报文")

    flat = flatten_payload(payload)
    if not flat:
        return ParseSampleResponse(
            parsed=False, fields=[], inferError="报文里没有找到可以识别的数据字段，请检查是否粘贴了完整内容"
        )

    if device.vendor_template_id:
        tpl = await vendor_template_repo.get(db, device.vendor_template_id)
        if tpl is not None and template_covers(tpl.field_spec, flat):
            return ParseSampleResponse(parsed=True, fields=fields_from_template(tpl.field_spec, flat))

    fields = infer_fields(flat)
    if not any_metric_inferred(fields):
        # 有字段但全是设备状态类（battery/rssi），识别不出环境指标：提示而非强推断。
        return ParseSampleResponse(
            parsed=False, fields=fields, inferError="只识别到设备状态字段，请确认粘贴的是环境数据报文"
        )
    return ParseSampleResponse(parsed=True, fields=fields)


__all__ = ["bind_device", "parse_sample"]
