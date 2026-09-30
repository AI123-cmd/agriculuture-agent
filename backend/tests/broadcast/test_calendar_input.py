"""T3-03：档案与农事历输入采集的用例。

覆盖四类验收点：两条流程排除、阶段推算与"不按月份兜底"、地区回退命中级标注、
`timing_rules` 与环境事实的联动。测试全部用构造的档案与桩检索实现，不起数据库——
这正是本节把数据库访问前移到调用方（`load_calendar_entries`）的目的。
"""

from datetime import date

from app.agent.contracts import EvidenceItem, EvidenceQuery
from app.agent.state import ProcessId
from app.broadcast.inputs.calendar import (
    NO_CALENDAR_PROCESSES,
    CalendarEntry,
    collect_calendar_tasks,
    query_calendar,
    resolve_stage,
)
from app.broadcast.inputs.env_facts import EnvFacts
from app.knowledge.region import RegionMatcher
from app.models import FarmerProfile

TODAY = date(2026, 6, 20)


def _profile(province: str | None = None, city: str | None = None, district: str | None = None) -> FarmerProfile:
    """构造一个内存档案（不落库），只填本项读取的地区字段。"""
    return FarmerProfile(
        id="fp-1",
        user_id="user-t303",
        real_name="测试农户",
        province=province,
        city=city,
        district=district,
    )


def _entry(
    process_id: ProcessId,
    *,
    start_date: date | None = date(2026, 6, 1),
    display_name: str = "东边水田",
    stage: str | None = "分蘖期",
    farm_entry_id: str = "crop-1",
) -> CalendarEntry:
    return CalendarEntry(
        process_id=process_id,
        start_date=start_date,
        display_name=display_name,
        stage=stage,
        farm_entry_id=farm_entry_id,
    )


class _StubSearcher:
    """桩检索：只在指定行政编码下返回条目，用于验证地区回退的落点。"""

    def __init__(self, hit_code: str | None, content: str) -> None:
        self._hit_code = hit_code
        self._content = content
        self.seen_codes: list[str | None] = []

    async def search(self, query: EvidenceQuery) -> list[EvidenceItem]:
        self.seen_codes.append(query.region_code)
        if query.region_code != self._hit_code:
            return []
        return [
            EvidenceItem(
                knowledge_id="K-RICE-0109",
                title="稻瘟病叶瘟防治指标",
                content=self._content,
                process_id=query.process_id.value,
                region_scope=None,
                effective_date=None,
                kind="threshold",
                score=0.9,
            )
        ]


# ---- 一、两条流程不参与农事历 ----


def test_no_calendar_processes_are_weed_and_general() -> None:
    assert NO_CALENDAR_PROCESSES == frozenset({ProcessId.WEED, ProcessId.GENERAL})


async def test_weed_and_general_entries_produce_no_calendar_items() -> None:
    searcher = _StubSearcher(hit_code="330102", content="分蘖期病叶率达 5% 即应施药")
    matcher = RegionMatcher(searcher)
    profile = _profile("浙江省", "杭州市", "上城区")
    entries = [_entry(ProcessId.WEED), _entry(ProcessId.GENERAL)]
    tasks = await collect_calendar_tasks(profile, TODAY, entries=entries, matcher=matcher, env=EnvFacts(source="sensor"))
    assert [t for t in tasks if t.source_kind == "calendar"] == []


async def test_rice_entry_in_tillering_produces_fact_with_stage_keyword() -> None:
    searcher = _StubSearcher(hit_code="330102", content="分蘖期病叶率达 5%，或田间出现急性型病斑时，即应施药防治。")
    matcher = RegionMatcher(searcher)
    profile = _profile("浙江省", "杭州市", "上城区")
    tasks = await collect_calendar_tasks(
        profile, TODAY, entries=[_entry(ProcessId.RICE, stage="分蘖期")], matcher=matcher, env=EnvFacts(source="sensor")
    )
    calendar_items = [t for t in tasks if t.source_kind == "calendar" and t.fact]
    assert calendar_items
    assert any("分蘖" in t.fact or "晒田" in t.fact for t in calendar_items)


# ---- 二、阶段推算：缺 start_date 返回 None，且不按月份兜底 ----


def test_resolve_stage_returns_none_when_start_date_missing() -> None:
    assert resolve_stage(_entry(ProcessId.RICE, start_date=None), TODAY) is None


def test_resolve_stage_returns_none_for_null_calendar_source() -> None:
    # 养猪的 calendar_source 为 null：没有生育期，由回访链路（T3-09）承担。
    assert resolve_stage(_entry(ProcessId.PIG), TODAY) is None


def test_resolve_stage_returns_none_before_start() -> None:
    future = _entry(ProcessId.RICE, start_date=date(2026, 7, 1))
    assert resolve_stage(future, TODAY) is None


def test_resolve_stage_returns_recorded_stage() -> None:
    assert resolve_stage(_entry(ProcessId.RICE, stage="分蘖期"), TODAY) == "分蘖期"


async def test_query_calendar_returns_empty_when_stage_is_none() -> None:
    searcher = _StubSearcher(hit_code="330102", content="分蘖期病叶率达 5%")
    matcher = RegionMatcher(searcher)
    items = await query_calendar(ProcessId.RICE, None, "330102", TODAY, matcher=matcher, entry=_entry(ProcessId.RICE))
    assert items == []
    # 未命中阶段时不得触碰检索：没有按月份反推出任何查询。
    assert searcher.seen_codes == []


# ---- 三、地区回退命中级与展示标注 ----


async def test_province_level_hit_records_hit_level_and_note() -> None:
    searcher = _StubSearcher(hit_code="330000", content="分蘖末期至孕穗期病丛率达 20% 时达标")
    matcher = RegionMatcher(searcher)
    items = await query_calendar(
        ProcessId.RICE, "分蘖期", "330102", TODAY, matcher=matcher, entry=_entry(ProcessId.RICE)
    )
    assert items
    provenance = items[0].facts_provenance
    # 来源标识形如 knowledge:K-RICE-0109:hit_level:province
    assert any(v == "hit_level:province" for v in provenance.values())
    assert any("hit_level:province" in f"{k}:{v}" for k, v in provenance.items())
    # 展示标注含"省"字（当前按浙江省通用提示）
    assert "省" in items[0].impact


async def test_national_fallback_marks_uncertain() -> None:
    searcher = _StubSearcher(hit_code="000000", content="全国通用的分蘖期管理要点")
    matcher = RegionMatcher(searcher)
    items = await query_calendar(
        ProcessId.RICE, "分蘖期", "330102", TODAY, matcher=matcher, entry=_entry(ProcessId.RICE)
    )
    assert items
    assert "通用提示" in items[0].impact
    assert any(v == "hit_level:national" for v in items[0].facts_provenance.values())


# ---- 四、timing_rules 与环境事实联动 ----


async def test_weather_timing_rule_skipped_when_env_none() -> None:
    # rice_rain_alert 的 when 是 {weather: continuous_rain}：环境不可用时必须跳过。
    profile = _profile("浙江省", "杭州市", "上城区")
    tasks = await collect_calendar_tasks(
        profile,
        TODAY,
        entries=[_entry(ProcessId.RICE, stage=None)],
        matcher=RegionMatcher(_StubSearcher(hit_code=None, content="")),
        env=EnvFacts(source="none"),
    )
    assert not any(t.rule_id == "calendar:rice_rain_alert" for t in tasks)


async def test_weather_timing_rule_produced_when_env_available() -> None:
    profile = _profile("浙江省", "杭州市", "上城区")
    tasks = await collect_calendar_tasks(
        profile,
        TODAY,
        entries=[_entry(ProcessId.RICE, stage=None)],
        matcher=RegionMatcher(_StubSearcher(hit_code=None, content="")),
        env=EnvFacts(source="weather", values={"rainfall": 30.0}),
    )
    assert any(t.rule_id == "calendar:rice_rain_alert" for t in tasks)


async def test_non_env_timing_rule_survives_env_none() -> None:
    # rice_boot_window 的 when 是 {growth_stage: booting}：非环境条件，不受环境可用性影响。
    profile = _profile("浙江省", "杭州市", "上城区")
    tasks = await collect_calendar_tasks(
        profile,
        TODAY,
        entries=[_entry(ProcessId.RICE, stage=None)],
        matcher=RegionMatcher(_StubSearcher(hit_code=None, content="")),
        env=EnvFacts(source="none"),
    )
    assert any(t.rule_id == "calendar:rice_boot_window" for t in tasks)


# ---- 五、callout 取对象名（多条目提示的对象可辨） ----


async def test_callout_uses_entry_display_name() -> None:
    profile = _profile("浙江省", "杭州市", "上城区")
    tasks = await collect_calendar_tasks(
        profile,
        TODAY,
        entries=[_entry(ProcessId.RICE, stage=None, display_name="东边水田")],
        matcher=RegionMatcher(_StubSearcher(hit_code=None, content="")),
        env=EnvFacts(source="none"),
    )
    assert tasks
    assert all(t.callout == "东边水田" for t in tasks)
