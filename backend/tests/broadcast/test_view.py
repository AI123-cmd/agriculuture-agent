"""T3-10 验收：跨日弱化与展示状态。

覆盖验收标准里的六个点：跨日弱化的三值状态（今天/昨天/三天前）、环境来源四级标注
（含 `none` + 地区回退命中级）、抑制条目不出现在展示列表但仍在库里、`urgent` 条目在
"昨日"快照下仍是最高视觉权重、`invalidated` 快照返回空状态、以及 `SnapshotView` 的字段
集合与技术方案 11.5 一致。

纯计算用例（`compute_display_status` / `build_env_note` / 视图装配）用内存构造 ORM 对象，
不需要数据库；"抑制条目仍落库"与"失效快照不返回"两条走真实 PostgreSQL。

数据访问一律走仓储（共用的 `session_factory` / `engine` 夹具），不出现 `db.query`。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.broadcast.view import (
    ENV_SOURCE_LABEL,
    PromptItemView,
    SnapshotView,
    build_env_note,
    build_snapshot_view,
    compute_display_status,
)
from app.db.base import new_id
from app.models import PromptItem, PromptSnapshot
from app.repositories import snapshot_repo

USER = "user-t310"
TODAY = date(2026, 5, 20)


def _snapshot(
    *,
    user_id: str = USER,
    generated_at: datetime,
    snapshot_date: date | None = None,
    env_source: str = "sensor",
    revision: int = 1,
    invalidated: bool = False,
    farm_entry_id: str | None = None,
) -> PromptSnapshot:
    return PromptSnapshot(
        id=new_id("snap"),
        user_id=user_id,
        farm_entry_id=farm_entry_id,
        snapshot_date=snapshot_date or generated_at.date(),
        generated_at=generated_at,
        env_source=env_source,
        revision=revision,
        invalidated=invalidated,
    )


def _item(
    *,
    user_id: str = USER,
    snapshot_id: str,
    severity: str = "notice",
    suppressed: bool = False,
    rule_id: str = "env_risk.heat",
    callout: str = "水稻·3亩",
) -> PromptItem:
    return PromptItem(
        id=new_id("item"),
        snapshot_id=snapshot_id,
        user_id=user_id,
        source_kind="env_risk",
        severity=severity,
        callout=callout,
        fact="近三日高温",
        impact="可能影响灌浆",
        action="早晚灌溉降温",
        facts_provenance={"air_temp": "sensor"},
        rule_id=rule_id,
        priority_score=10,
        suppressed=suppressed,
        executed=False,
    )


def _at(day: date, hour: int = 7, minute: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=hour, minute=minute)


async def _persist(engine, *objects: object) -> None:
    """逐对象 `add` + `flush` 落库：表间只有外键、无 ORM relationship，需保证落库顺序。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        for obj in objects:
            db.add(obj)
            await db.flush()
        await db.commit()


# ---- 一、跨日弱化的三值状态 ----


@pytest.mark.parametrize(
    ("generated_at", "expected"),
    [
        (_at(TODAY), ("today", 0)),
        (_at(TODAY - timedelta(days=1)), ("yesterday", 1)),
        (_at(TODAY - timedelta(days=3)), ("stale", 3)),
    ],
)
def test_compute_display_status_three_values(generated_at: datetime, expected: tuple[str, int]) -> None:
    """今天 / 昨天 / 三天前分别为 today / yesterday / stale，天数同步。"""
    assert compute_display_status(generated_at, TODAY) == expected


def test_compute_display_status_future_counts_as_today() -> None:
    """未来时间（时钟回拨/时区切换）按"今天"处理，比按"未来"展示更安全。"""
    assert compute_display_status(_at(TODAY + timedelta(days=2)), TODAY) == ("today", 0)


# ---- 二、环境来源四级标注 ----


def test_env_note_sensor() -> None:
    assert "本地传感器" in build_env_note("sensor", None, None)


def test_env_note_sensor_stale_includes_hours() -> None:
    """陈旧传感器：文案要含"小时前"，且小时数从时效标注里规整提取。"""
    note = build_env_note("sensor_stale", "数据为 3.5 小时前", None)
    assert "小时前" in note
    assert "3 小时前" in note


def test_env_note_sensor_stale_missing_note_falls_back() -> None:
    """取不到小时数时不臆造数字，退化为不带时效的本地传感器文案。"""
    note = build_env_note("sensor_stale", None, None)
    assert note == ENV_SOURCE_LABEL["sensor"]
    assert "小时前" not in note


def test_env_note_weather() -> None:
    assert "公共天气" in build_env_note("weather", None, None)


def test_env_note_none() -> None:
    assert "基于你的描述" in build_env_note("none", None, None)


def test_env_note_none_with_region_hit_level() -> None:
    """无环境数据但地区回退命中：补一句"按 XX 通用口径给出"，交代农时提示的地域依据。"""
    note = build_env_note("none", None, "市级")
    assert "基于你的描述" in note
    assert "市级通用口径" in note


# ---- 三、抑制条目过滤（内存装配） ----


def test_snapshot_view_excludes_suppressed_items() -> None:
    """展示列表只含 suppressed=False 的条目，被压制条目不进 items。"""
    snapshot = _snapshot(generated_at=_at(TODAY))
    kept = _item(snapshot_id=snapshot.id, rule_id="env_risk.heat")
    dropped = _item(snapshot_id=snapshot.id, rule_id="env_risk.cold", suppressed=True)

    view = build_snapshot_view(snapshot, today=TODAY, items=[kept, dropped])

    assert view is not None
    rule_ids = {item.rule_id for item in view.items}
    assert rule_ids == {"env_risk.heat"}


# ---- 四、紧急条目强化 ----


def test_urgent_item_is_high_emphasis_on_yesterday_snapshot() -> None:
    """昨日快照里的 urgent 条目仍为最高视觉权重（不随跨日弱化）。"""
    snapshot = _snapshot(generated_at=_at(TODAY - timedelta(days=1)))
    urgent = _item(snapshot_id=snapshot.id, severity="urgent", rule_id="env_risk.storm")
    normal = _item(snapshot_id=snapshot.id, severity="notice", rule_id="calendar.sow")

    view = build_snapshot_view(snapshot, today=TODAY, items=[urgent, normal])

    assert view is not None
    assert view.display_status == "yesterday"
    emphasis = {item.rule_id: item.emphasis for item in view.items}
    assert emphasis["env_risk.storm"] == "high"
    assert emphasis["calendar.sow"] == "normal"


# ---- 五、失效快照 ----


def test_invalidated_snapshot_returns_none() -> None:
    """invalidated=True 的快照不返回内容，视同当日尚未生成（接口据此返回空状态）。"""
    snapshot = _snapshot(generated_at=_at(TODAY), invalidated=True)
    assert build_snapshot_view(snapshot, today=TODAY, items=[]) is None


# ---- 六、契约字段与别名 ----


def test_snapshot_view_has_required_fields() -> None:
    """与技术方案 11.5 定义一致：四字段齐备。"""
    fields = set(SnapshotView.model_fields)
    assert {"items", "generated_at", "display_status", "days_ago"} <= fields


def test_view_serializes_to_camel_case() -> None:
    """接口输出小驼峰：内部蛇形属性在 schema 边界转换。"""
    snapshot = _snapshot(generated_at=_at(TODAY))
    view = build_snapshot_view(snapshot, today=TODAY, items=[_item(snapshot_id=snapshot.id)])
    assert view is not None

    payload = view.model_dump(mode="json", by_alias=True)
    assert set(payload) >= {"items", "generatedAt", "displayStatus", "daysAgo", "snapshotId"}
    assert payload["displayStatus"] == "today"
    assert payload["daysAgo"] == 0
    item_payload = payload["items"][0]
    assert "itemId" in item_payload
    assert "sourceKind" in item_payload
    assert item_payload["emphasis"] == "normal"


def test_prompt_item_view_alias_and_fields() -> None:
    """条目视图含 emphasis，别名转小驼峰。"""
    item = _item(snapshot_id="snap-x", severity="urgent")
    view = PromptItemView.model_validate(
        {
            "item_id": item.id,
            "source_kind": item.source_kind,
            "severity": item.severity,
            "callout": item.callout,
            "fact": item.fact,
            "impact": item.impact,
            "action": item.action,
            "executed": item.executed,
            "rule_id": item.rule_id,
            "emphasis": "high",
        }
    )
    assert view.model_dump(by_alias=True)["ruleId"] == item.rule_id


# ---- 七、抑制条目仍落库（真实 PostgreSQL） ----


@pytest.mark.usefixtures("requires_postgres")
async def test_suppressed_item_persists_in_db_but_excluded_from_view(engine) -> None:
    """suppressed=True 的条目存在于数据库，但不进展示列表。"""
    snapshot = _snapshot(generated_at=_at(TODAY))
    kept = _item(snapshot_id=snapshot.id, rule_id="env_risk.heat")
    dropped = _item(snapshot_id=snapshot.id, rule_id="env_risk.cold", suppressed=True)
    await _persist(engine, snapshot, kept, dropped)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        rows: Sequence[PromptItem] = await snapshot_repo.list_items(db, USER, snapshot.id)
        view = build_snapshot_view(snapshot, today=TODAY, items=rows)

    assert {row.rule_id for row in rows} == {"env_risk.heat", "env_risk.cold"}
    persisted = next(row for row in rows if row.rule_id == "env_risk.cold")
    assert persisted.suppressed is True

    assert view is not None
    assert {item.rule_id for item in view.items} == {"env_risk.heat"}
