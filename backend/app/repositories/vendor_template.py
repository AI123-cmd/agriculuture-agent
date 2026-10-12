"""厂商模板仓储（T5-02）。

模板是平台侧公共资产，**不带归属**，因此本仓储不继承 `OwnedRepository`（该基类要求模型有
`user_id`），改用不带归属条件的普通查询。这是对"所有查询强制注入 user_id"的一处**刻意例外**：
`vendor_template` 表本身没有 `user_id` 列，农户读取模板不构成越权（模板对所有人相同）。

`match()` 是三步接入的关键入口（T5-03 绑定判模板、T5-04 粘贴解析）：两层匹配——先按型号
字符串，再按报文结构特征，最后落通用模板。
"""

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VendorTemplate

# 结构签名 → 模板名的提示。签名来自报文顶层键的组合，见 `_payload_shape`。
_SHAPE_KEY_METHOD_PARAMS = "method+params"
_SHAPE_KEY_DP = "dp"
_SHAPE_KEY_DATA = "data"

# 通用兜底模板的固定 id（`vendor_templates.json` 中的 `tpl_generic_http`）。
GENERIC_TEMPLATE_ID = "tpl_generic_http"


def _payload_shape(payload: dict[str, Any]) -> str | None:
    """返回报文的结构签名。

    按结构匹配比按字段名匹配稳健：底层平台不同但报文结构相同的设备会得到同一个模板，这正是
    模板能覆盖"多数农户"的实现方式。三档签名与调研报告 9.4 的三类平台报文一一对应。
    """
    keys = set(payload)
    if {"method", "params"} <= keys:
        return _SHAPE_KEY_METHOD_PARAMS
    if "dp" in keys:
        return _SHAPE_KEY_DP
    if "data" in keys:
        return _SHAPE_KEY_DATA
    return None


def _template_shape(sample_payload: str) -> str | None:
    """把模板的样例报文解析成结构签名；样例不可解析时返回 `None`。"""
    try:
        parsed = json.loads(sample_payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return _payload_shape(parsed)


class VendorTemplateRepository:
    """厂商模板仓储：两层匹配 + 通用兜底。"""

    async def list_all(self, db: AsyncSession) -> list[VendorTemplate]:
        """全部模板。模板是平台公共资产，不按归属过滤。"""
        return list((await db.execute(select(VendorTemplate))).scalars().all())

    async def get(self, db: AsyncSession, template_id: str) -> VendorTemplate | None:
        """按 id 取模板（T5-04 读 `field_spec` 时用）。"""
        return await db.get(VendorTemplate, template_id)

    async def match(
        self, db: AsyncSession, payload: dict[str, Any], model_hint: str | None = None
    ) -> VendorTemplate | None:
        """两层匹配：先按型号字符串，再按报文结构特征，最后落通用模板。

        `model_hint` 为可选参数，仅在小程序侧能取到型号（如扫码带出）时传入。型号匹配排在结构
        匹配之前：型号是厂商给定的确定信息，优先于"报文长得像"这种旁证。
        """
        templates = await self.list_all(db)

        if model_hint:
            for tpl in templates:
                if tpl.model_pattern and tpl.id != GENERIC_TEMPLATE_ID and re.fullmatch(tpl.model_pattern, model_hint):
                    return tpl

        shape = _payload_shape(payload)
        if shape is not None:
            for tpl in templates:
                if tpl.id == GENERIC_TEMPLATE_ID:
                    continue
                if _template_shape(tpl.sample_payload) == shape:
                    return tpl

        return self._find_generic(templates)

    @staticmethod
    def _find_generic(templates: list[VendorTemplate]) -> VendorTemplate | None:
        """通用兜底：`tpl_generic_http`。找不到时返回 `None`（模板库为空）。"""
        for tpl in templates:
            if tpl.id == GENERIC_TEMPLATE_ID:
                return tpl
        return None


vendor_template_repo = VendorTemplateRepository()

__all__ = ["GENERIC_TEMPLATE_ID", "VendorTemplateRepository", "vendor_template_repo"]
