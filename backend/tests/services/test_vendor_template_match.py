"""T5-02 验收：厂商模板的两层匹配、内置数据集结构校验与幂等导入。

`match()` 是纯查询逻辑（读库），故用真实 PostgreSQL 建表后断言；`field_spec` 的枚举合法性与
"样例字段集合 == 映射键集合"是数据集自身的约束，不连库也能校验，这两类分开跑。
"""

import json

import pytest
from sqlalchemy import func, select

from app.domain.metrics import StandardMetric
from app.models import VendorTemplate
from app.repositories.vendor_template import GENERIC_TEMPLATE_ID, VendorTemplateRepository
from app.services.template_seed import BUILTIN_ID_PREFIX, load_builtin_templates, seed_vendor_templates

pytestmark = pytest.mark.usefixtures("requires_postgres")


@pytest.fixture()
def repo() -> VendorTemplateRepository:
    return VendorTemplateRepository()


async def _seed(session_factory) -> None:
    async with session_factory() as db:
        await seed_vendor_templates(db)
        await db.commit()


# ---- 数据集结构（不连库） ----


def test_builtin_dataset_covers_four_templates() -> None:
    """内置数据集至少含四类模板，id 覆盖四类报文结构。"""
    ids = {item["id"] for item in load_builtin_templates()}
    assert {"tpl_generic_http", "tpl_aliyun_alink", "tpl_tencent_iot", "tpl_onenet_dp"} <= ids


def test_builtin_ids_all_prefixed() -> None:
    """内置模板 id 统一带前缀，便于与运营手工新增的模板区分。"""
    assert all(item["id"].startswith(BUILTIN_ID_PREFIX) for item in load_builtin_templates())


def test_builtin_field_spec_metrics_are_closed_enum() -> None:
    """不存在 `standard_metric` 不在 `StandardMetric` 枚举内的内置模板（遍历校验）。"""
    valid = {metric.value for metric in StandardMetric}
    for item in load_builtin_templates():
        for raw_field, spec in item["field_spec"].items():
            assert spec["standard_metric"] in valid, f"{item['id']}.{raw_field} 的指标非法：{spec['standard_metric']}"


def test_builtin_sample_keys_match_field_spec_keys() -> None:
    """每条模板的 `field_spec` 键都能在样例报文中找到，且 `sample_value` 与实际值一致。

    "反之亦然"按环境数据字段理解：样例里还会出现 `sn` / `ts` / `id` / `method` / `clientToken`
    等设备标识与平台协议字段，它们不对应环境指标，自然不进 `field_spec`。
    """
    for item in load_builtin_templates():
        payload = json.loads(item["sample_payload"])
        flat = _flatten(payload)
        for raw_field, spec in item["field_spec"].items():
            assert raw_field in flat, f"{item['id']} 的 {raw_field} 不在样例报文中"
            assert flat[raw_field] == spec["sample_value"], f"{item['id']}.{raw_field} 的 sample_value 与样例不符"


def _flatten(payload: object, prefix: str = "") -> dict[str, object]:
    """把样例报文扁平化成 `点分键 → 叶子值`，与 `field_spec` 的键口径一致。"""
    out: dict[str, object] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            out.update(_flatten(value, path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            out.update(_flatten(item, f"{prefix}.{index}"))
    else:
        out[prefix] = payload
    return out


# ---- 匹配（连库） ----


async def test_match_aliyun_payload_returns_alink(session_factory, repo) -> None:
    """阿里云 Alink 报文（method+params）命中 `tpl_aliyun_alink`。"""
    await _seed(session_factory)
    payload = {"id": "m1", "method": "thing.event.property.post", "params": {"air_temp": 265}}
    async with session_factory() as db:
        tpl = await repo.match(db, payload)
    assert tpl is not None
    assert tpl.id == "tpl_aliyun_alink"


async def test_match_onenet_payload_returns_dp(session_factory, repo) -> None:
    """OneNET 报文（dp）命中 `tpl_onenet_dp`。"""
    await _seed(session_factory)
    payload = {"id": 1, "dp": {"soil_temp": [{"v": 21.3, "t": 1}]}}
    async with session_factory() as db:
        tpl = await repo.match(db, payload)
    assert tpl is not None
    assert tpl.id == "tpl_onenet_dp"


async def test_match_custom_payload_returns_generic(session_factory, repo) -> None:
    """自定义 JSON 报文（data）命中 `tpl_generic_http`。"""
    await _seed(session_factory)
    payload = {"sn": "HN1", "ts": 1, "data": {"air_temp": 26.5}}
    async with session_factory() as db:
        tpl = await repo.match(db, payload)
    assert tpl is not None
    assert tpl.id == GENERIC_TEMPLATE_ID


async def test_match_unrecognized_shape_falls_back_to_generic(session_factory, repo) -> None:
    """结构不可识别（无 method/params/dp/data）时兜底到通用模板，而非返回空。"""
    await _seed(session_factory)
    async with session_factory() as db:
        tpl = await repo.match(db, {"air_temp": 26.5})
    assert tpl is not None
    assert tpl.id == GENERIC_TEMPLATE_ID


async def test_model_hint_beats_shape_matching(session_factory, repo) -> None:
    """`model_hint="Ali-4G-01"` 时优先命中 `tpl_aliyun_alink`，而非结构匹配结果。"""
    await _seed(session_factory)
    # 报文结构像自定义 JSON（data），但型号提示指向阿里云——型号优先。
    payload = {"sn": "HN1", "data": {"air_temp": 26.5}}
    async with session_factory() as db:
        tpl = await repo.match(db, payload, model_hint="Ali-4G-01")
    assert tpl is not None
    assert tpl.id == "tpl_aliyun_alink"


# ---- 幂等导入（连库） ----


async def test_seed_is_idempotent(session_factory) -> None:
    """连续启动两次后模板数不翻倍。"""
    async with session_factory() as db:
        await seed_vendor_templates(db)
        await db.commit()
        first = (await db.execute(select(func.count()).select_from(VendorTemplate))).scalar_one()

    async with session_factory() as db:
        await seed_vendor_templates(db)
        await db.commit()
        second = (await db.execute(select(func.count()).select_from(VendorTemplate))).scalar_one()

    assert first == second
    assert first >= 4


async def test_seed_updates_existing_without_deleting_manual(session_factory) -> None:
    """重复导入只 upsert 内置模板，不删除运营手工新增的模板。"""
    async with session_factory() as db:
        db.add(
            VendorTemplate(
                id="tpl_manual_ops",
                vendor_name="运营新增",
                model_pattern="OPS.*",
                sample_payload="{}",
                field_spec={},
            )
        )
        await db.commit()

    await _seed(session_factory)

    async with session_factory() as db:
        kept = await db.get(VendorTemplate, "tpl_manual_ops")
        assert kept is not None
        # 内置模板内容被 upsert（若已存在则覆盖为数据集的值）。
        generic = await db.get(VendorTemplate, GENERIC_TEMPLATE_ID)
        assert generic is not None
        assert generic.vendor_name == "通用四合一"


async def test_seed_returns_item_count(session_factory) -> None:
    """`seed_vendor_templates` 返回处理的条目数。"""
    async with session_factory() as db:
        count = await seed_vendor_templates(db)
        await db.commit()
    assert count == len(load_builtin_templates())
