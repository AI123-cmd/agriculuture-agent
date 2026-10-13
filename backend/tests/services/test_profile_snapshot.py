"""P0-B 验收：档案 → 问诊图的快照桥接（`AIService.build_profile_snapshot`）。

图内 `triage` / `candidate_pool` / 分诊提示词读的是 `profile` 系列契约键，而不是展示用的
`context`。本用例把这座桥的产出逐条钉死，覆盖三种起点：

1. **有档案有作物**：`profile` 带 `location` / `entries` / `object_label` / `slot_values`，
   `profile_process_id` / `profile_process_hint` 归一为主作物流程，`prescription_history`
   显式给空列表（表示"暂无已知用药史"这一事实，而非占位）。
2. **阶段值填充口径**：只有档案阶段确实落在该槽位 `options` 内才写入 `slot_values`——
   写错口径会让筛选器据此剔除候选，农户永远问不到那个问题。
3. **无档案且无作物**：`profile` 给空字典，保 `triage.degrade` 的 `profile_ready=bool(profile)`
   如实为假。

需要真实 PostgreSQL：断言落在 `farmer_profile` / `crop` 的真实读写与归属过滤上。
"""

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Crop, FarmerProfile, User
from app.services.ai_service import AIService

pytestmark = pytest.mark.usefixtures("requires_postgres")

USER = "user-p0b"


async def _seed(engine, *, crops: list[Crop], with_profile: bool = True) -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        db.add(User(id=USER, openid="openid-p0b"))
        await db.flush()
        if with_profile:
            db.add(
                FarmerProfile(
                    id="fp-p0b",
                    user_id=USER,
                    real_name="王五",
                    province="江西省",
                    city="抚州市",
                    district="临川区",
                    total_mu=12.5,
                )
            )
        for crop in crops:
            crop.user_id = USER
            db.add(crop)
        await db.commit()


async def _snapshot(engine) -> dict:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        return await AIService().build_profile_snapshot(db, USER)


async def test_snapshot_shape_for_profile_with_crop(engine) -> None:
    """有档案有作物：四处契约键齐全，主作物流程归一正确。"""
    await _seed(
        engine,
        crops=[
            Crop(
                id="crop-rice",
                crop_name="水稻",
                plant_mu=12.5,
                sowing_date=date(2026, 4, 10),
                growth_stage="分蘖期",
                is_main=1,
            )
        ],
    )

    snap = await _snapshot(engine)

    profile = snap["profile"]
    assert profile["location"] == "江西省抚州市临川区"
    assert profile["entries"] == [
        {"process_id": "rice", "start_date": "2026-04-10", "display_name": "水稻"}
    ]
    assert profile["object_label"] == "水稻·12.5亩"
    assert snap["profile_process_id"] == "rice"
    assert snap["profile_process_hint"] == "rice"
    assert snap["prescription_history"] == []


async def test_growth_stage_fills_matching_slot_only(engine) -> None:
    """阶段值落在该流程槽位 `options` 内才写入 `slot_values`，否则留空照问。"""
    await _seed(
        engine,
        crops=[
            Crop(id="crop-rice", crop_name="水稻", plant_mu=3, growth_stage="分蘖期", is_main=1)
        ],
    )
    matched = await _snapshot(engine)
    assert matched["profile"]["slot_values"] == {"rice_growth_stage": "分蘖期"}


async def test_offvocabulary_stage_is_not_filled(engine) -> None:
    """阶段词不在流程选项内：不写入 `slot_values`，宁可多问一句也不写伪造值。"""
    await _seed(
        engine,
        crops=[
            # "旺长期"是烟草的阶段词，不属于水稻流程的 options
            Crop(id="crop-rice", crop_name="水稻", plant_mu=3, growth_stage="旺长期", is_main=1)
        ],
    )

    snap = await _snapshot(engine)

    assert snap["profile"]["slot_values"] == {}
    assert snap["profile_process_id"] == "rice"


async def test_no_profile_and_no_crop_yields_empty_profile(engine) -> None:
    """无档案无作物：`profile` 为空字典，保 `profile_ready` 语义正确。"""
    await _seed(engine, crops=[], with_profile=False)

    snap = await _snapshot(engine)

    assert snap["profile"] == {}
    assert snap["profile_process_id"] is None
    assert snap["profile_process_hint"] is None
    assert snap["prescription_history"] == []


async def test_unknown_crop_yields_no_process(engine) -> None:
    """作物名匹配不到任何流程别名：`entries` 仍有该条目，但 `process_id` 为 `None`。"""
    await _seed(
        engine,
        crops=[Crop(id="crop-x", crop_name="番石榴", plant_mu=1, is_main=1)],
    )

    snap = await _snapshot(engine)

    assert snap["profile_process_id"] is None
    assert snap["profile"]["entries"] == [
        {"process_id": None, "start_date": None, "display_name": "番石榴"}
    ]
    # 有档案有作物，故不是空字典
    assert snap["profile"]["object_label"] == "番石榴·1亩"
