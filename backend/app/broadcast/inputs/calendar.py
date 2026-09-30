"""档案与农事历输入采集（T3-03，技术方案 11.2）。

本模块把"农户档案 + 今天"变成一组农事历节点提示。设计上有三条硬约束：

1. **两条流程不参与**：杂草防治没有生育期概念，通用农业无法确定生育期模型，二者只保留
   环境风险类与回访类提示（产品说明书 4.3.2）。这两个取值集中在 `NO_CALENDAR_PROCESSES`，
   写在模块顶层以便被测试直接引用，也让"哪两条流程不参与"可被 grep。
2. **生育期是唯一的时间轴，且不得用月份反推**：`resolve_stage` 返回 `None` 时
   `query_calendar` 必须直接返回空列表。暖冬年份实际处于分蘖期而按月份推算出"抽穗期
   施穗肥"这类错误提示，比没有提示危害更大。
3. **地区回退保留实际命中级**：检索按县级 → 市级 → 省级 → 全国逐级回退，命中级别写入
   `facts_provenance`，供界面标注"当前按 XX 省通用农时提示"（全局约定第六节）。

关于"经营条目"与"地区"的落地口径（旧接口对接新接口）：技术方案 11.2 的示例代码写作
`profile.entries` 与 `profile.region`，但 `FarmerProfile` 并无这两个属性，仓库里也没有
`FarmEntry` 表。因此本模块定义值对象 `CalendarEntry`，由 `load_calendar_entries` 从既有的
`crop` 表桥接（`Crop.crop_name` 经 `object_aliases` → 流程、`Crop.sowing_date` → 起始时间、
`Crop.growth_stage` → 已记录的生育阶段），地区则从档案的省/市/县名反查 `regions.json` 得到
行政编码。这样既不改数据库结构，也保留了"读路径走仓储层、`collect_calendar_tasks` 本身保持
纯筛选、可用构造档案直接单测"的设计意图。

养殖流程（养猪）没有生育期但有法定时限链，其农时节点由回访链路（T3-09）承担，不在本项范围；
`resolve_stage` 对 `calendar_source` 为 `null` 的流程一律返回 `None`，不在此处为养猪添加分支。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contracts import EvidenceQuery
from app.agent.contracts.triage import match_object_alias
from app.agent.state import ProcessId
from app.broadcast.inputs.env_facts import EnvFacts
from app.broadcast.rules.base import RuleResult
from app.domain.metrics import StandardMetric
from app.knowledge.region import RegionMatcher
from app.knowledge.region_codes import load_region_names, parent_code, region_name
from app.knowledge.schema import NATIONAL_LEVEL
from app.models import Crop, FarmerProfile
from app.process_config.errors import ConfigLoadError
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.models import ProcessConfig

# 不参与农事历的两条流程（产品说明书 4.3.2）。这是"无生育期概念"这句话的代码形式：
# 杂草防治不绑定地块、通用农业无流程专属生育期模型。定义在模块顶层以便测试直接引用与 grep。
NO_CALENDAR_PROCESSES: frozenset[ProcessId] = frozenset({ProcessId.WEED, ProcessId.GENERAL})

# 农事历条目在提示快照里的来源标记（T3-01）。
CALENDAR_SOURCE_KIND = "calendar"

# 判定 `timing_rules.when` 是否含环境条件的指标前缀集合。从 `StandardMetric` 派生而非逐条
# 硬编码条件名：新增一个环境指标时这里自动生效，不会出现"配置写了但没被识别为环境条件"。
_ENV_METRIC_PREFIXES: tuple[str, ...] = tuple(metric.value for metric in StandardMetric)

# 展示标注不需要出现的命中级：县级命中说明有专属条目，未命中无话可说（与
# `RegionSearchResult.display_note` 的抑制集合保持一致）。
_NOTE_SUPPRESSED = frozenset({"county", "none"})


@dataclass(frozen=True)
class CalendarEntry:
    """一条经营条目在农事历输入里的最小形态。

    `display_name` 是对象名（如"东边水田"），进 `callout`——单块地的农户看到"土壤墒情不足"
    能理解，多块地的农户不能，必须看到"东边水田：土壤墒情不足"（T3-03 第六节）。
    `farm_entry_id` 进 `facts_provenance`，使"这条提示属于哪块地"可被追溯。
    """

    process_id: ProcessId
    start_date: date | None
    display_name: str
    stage: str | None = None
    farm_entry_id: str | None = None


def _get_config(process_id: ProcessId) -> ProcessConfig | None:
    """取流程配置；配置不可用时返回 `None`（由调用方决定跳过）。"""
    try:
        return get_process_config(process_id)
    except ConfigLoadError:
        return None


def _is_env_condition(when: dict) -> bool:
    """`timing_rule.when` 是否引用了环境条件。

    判定方式是"键是否落在环境指标集合内"（`weather` 或某个 `StandardMetric` 前缀，如
    `air_temp_above`、`soil_temp_above`），而不是逐条硬编码条件名——后者每加一个指标都要
    改这里。非环境条件（`growth_stage`、`season`）不受环境事实可用性影响。
    """
    for key in when:
        if key == "weather" or key.startswith(_ENV_METRIC_PREFIXES):
            return True
    return False


def resolve_stage(entry: CalendarEntry, today: date) -> str | None:
    """由流程类型与起始时间推算当前生育阶段；算不出即返回 `None`。

    `resolve_stage(entry, today)` 是技术方案 11.2 的签名。被推算的形态由流程配置的
    `calendar_source` 决定（`rice.growth_stage` / `tea.picking_round` 走生育阶段推算，
    `null` 直接返回 `None`）——`calendar_source` 从 `get_process_config(entry.process_id)`
    读取，使本函数不需要调用方额外传参。

    两条前置条件缺一不可，任一不满足即 `None`：

    - `start_date` 缺失：起始时间未知，无法把条目定位到时间轴（T3-03 第五节"有条目但缺
      start_date → 该条目不产出农事历提示，追问补时间"）。
    - `start_date` 晚于 `today`：条目尚未开始，不产出提示。

    阶段本身取条目已记录的生育阶段（来自 `Crop.growth_stage`）。仓库中没有任何"生育期天数
    区间"数据源，因此本函数**绝不用当前月份反推阶段**——用月份猜阶段在气候异常年份会产出
    明显错误的农时提示（暖冬年份实际处于分蘖期却推成抽穗期施穗肥），危害大于不给提示。
    `today` 预留给将来把生育期天数区间纳入流程配置后的窗口推算。
    """
    config = _get_config(entry.process_id)
    if config is None or config.calendar_source is None:
        return None
    if entry.start_date is None:
        return None
    if entry.start_date > today:
        return None
    return entry.stage or None


def _region_code_of(profile: FarmerProfile) -> str | None:
    """把档案的省/市/县名反查成六位行政编码，越具体越优先。

    `regions.json` 只有"编码 → 名称"一个方向，这里据它构造反查表。反查不到时返回 `None`，
    由回退链落到全国一级——不做任何模糊匹配（错误的具体地区比全国通用更危险）。
    """
    by_name = {name: code for code, name in load_region_names().items()}
    for name in ((profile.district or "").strip(), (profile.city or "").strip(), (profile.province or "").strip()):
        code = by_name.get(name)
        if code:
            return code
    return None


def _display_region_name(region_code: str | None, level: str) -> str:
    """给出实际命中级的展示名，供标注语使用。全国一级固定为"全国"。"""
    if level == NATIONAL_LEVEL or region_code is None:
        return "全国"
    return region_name(parent_code(region_code, level))


def _region_note(region_code: str | None, level: str) -> str | None:
    """生成界面的地区标注语；县级命中或未命中时不标注（全局约定第六节）。

    只有回退到市级及以上时才标注：县级命中说明农户所在地有专属条目，界面无需额外说明。
    """
    if level in _NOTE_SUPPRESSED:
        return None
    return f"当前按{_display_region_name(region_code, level)}通用提示"


async def query_calendar(
    process_id: ProcessId,
    stage: str | None,
    region_code: str | None,
    today: date,
    *,
    matcher: RegionMatcher,
    entry: CalendarEntry,
) -> list[RuleResult]:
    """按生育阶段检索农事历条目，带地区回退；`stage` 为 `None` 时返回空列表。

    **硬约束**：`stage` 为 `None` 即返回空列表，绝不退化成"用当前月份猜一个阶段"（技术方案
    11.2）。检索走 T2-49 的混合检索与 T2-50 的地区回退，命中级别写入 `facts_provenance`，
    来源标识形如 `knowledge:K-RICE-0109:hit_level:city`。
    """
    if stage is None:
        return []

    query = EvidenceQuery(
        process_id=process_id,
        keywords=[stage],
        region_code=region_code,
        kind_filter=["cultivation", "threshold"],
    )
    result = await matcher.search_with_fallback(query)
    if not result.items:
        # 县级到全国全部未命中：不编造条目，返回空（该路提示不生成，不阻断其他两路）。
        return []

    note = _region_note(region_code, result.region_level_actual)
    items: list[RuleResult] = []
    for item in result.items:
        impact = note or f"当前处于{stage}"
        if result.region_level_actual == NATIONAL_LEVEL:
            # 只有全国通用条目可用：按产品说明书 4.2.4 标注不确定，而不是伪装成地区专属提示。
            impact = f"{impact}（通用提示，仅供参考）"
        items.append(
            RuleResult(
                source_kind=CALENDAR_SOURCE_KIND,
                severity="notice",
                callout=entry.display_name,
                fact=item.content,
                impact=impact,
                action=item.title,
                rule_id=f"calendar:{item.knowledge_id}",
                facts_provenance={
                    f"knowledge:{item.knowledge_id}": f"hit_level:{result.region_level_actual}",
                    "farm_entry_id": entry.farm_entry_id or "",
                },
                relevance_score=item.score,
            )
        )
    return items


def _timing_rule_items(
    config: ProcessConfig,
    entry: CalendarEntry,
    stage: str | None,
    env: EnvFacts | None,
) -> list[RuleResult]:
    """把流程配置的 `timing_rules` 纳入农事历输入：`id` 生成 `rule_id`，`prompt` 生成 `fact`。

    文案来自流程配置而非代码拼接，新增一条时令提示的改动量是"在 YAML 里加一行"。
    引用天气/环境条件的条目与环境事实联动：`EnvFacts.source == "none"`（或未提供环境事实）时
    一律跳过，理由与环境风险类一致——数据不可用时不用推测值生成。
    """
    env_unavailable = env is None or env.source == "none"
    items: list[RuleResult] = []
    for rule in config.timing_rules:
        if env_unavailable and _is_env_condition(rule.when):
            continue
        items.append(
            RuleResult(
                source_kind=CALENDAR_SOURCE_KIND,
                severity="reminder",
                callout=entry.display_name,
                fact=rule.prompt,
                impact=f"当前处于{stage}" if stage else "当前处于该时令窗口",
                action="按该时令要点安排农事",
                rule_id=f"calendar:{rule.id}",
                facts_provenance={"timing_rule": rule.id, "farm_entry_id": entry.farm_entry_id or ""},
            )
        )
    return items


async def collect_calendar_tasks(
    profile: FarmerProfile,
    today: date,
    *,
    entries: Sequence[CalendarEntry] = (),
    env: EnvFacts | None = None,
    matcher: RegionMatcher | None = None,
    configs: dict[ProcessId, ProcessConfig] | None = None,
) -> list[RuleResult]:
    """农事历节点提示：杂草防治与通用农业不生成此类提示。

    一个农户可能同时有多条经营条目（多块地、多种作物），本函数产出所有条目的并集；每条提示
    的 `callout` 一律取条目的显示名而非流程名，`facts_provenance` 携带条目标识。

    排除用逐条目 `continue` 而不是在循环外过滤：通用农业条目可能存在于档案中，必须在循环内
    排除，否则一条通用条目会把整户的农事历提示都带偏（T3-03 第一节）。

    `entries` 由调用方注入（T3-08 用 `load_calendar_entries` 从 `crop` 表桥接），使本函数保持
    纯筛选、不需要数据库即可单测——与技术方案 11.2"把数据库访问前移到调用方"一致。
    """
    configs = configs if configs is not None else load_all_configs()
    matcher = matcher if matcher is not None else RegionMatcher()
    region_code = _region_code_of(profile)
    tasks: list[RuleResult] = []

    for entry in entries:
        if entry.process_id in NO_CALENDAR_PROCESSES:
            continue  # 产品说明书 4.3.2：无生育期概念，不生成农时节点类提示
        config = configs.get(entry.process_id)
        if config is None:
            continue  # 配置不可用的流程不产出提示（由路由层标记不可用）
        stage = resolve_stage(entry, today)
        if stage is not None:
            tasks += await query_calendar(entry.process_id, stage, region_code, today, matcher=matcher, entry=entry)
        tasks += _timing_rule_items(config, entry, stage, env)
    return tasks


async def load_calendar_entries(db: AsyncSession, user_id: str) -> list[CalendarEntry]:
    """从既有 `crop` 表桥接出经营条目列表（旧接口对接新接口的落地处）。

    读取路径走 T1-05 的 `OwnedRepository`（`crop_repo.list_for_profile`），`user_id` 由调用方
    （T3-08）传入且不可为空。作物名经 `object_aliases` 最长别名匹配到流程；匹配不到的作物
    不是任何一条流程的经营条目，跳过（不臆测流程归属）。
    """
    from app.repositories import crop_repo

    configs = load_all_configs()
    crops: Sequence[Crop] = await crop_repo.list_for_profile(db, user_id)
    entries: list[CalendarEntry] = []
    for crop in crops:
        process_id = match_object_alias(crop.crop_name or "", configs)
        if process_id is None:
            continue
        entries.append(
            CalendarEntry(
                process_id=process_id,
                start_date=crop.sowing_date,
                display_name=crop.crop_name or "",
                stage=crop.growth_stage or None,
                farm_entry_id=crop.id,
            )
        )
    return entries


__all__ = [
    "CALENDAR_SOURCE_KIND",
    "NO_CALENDAR_PROCESSES",
    "CalendarEntry",
    "collect_calendar_tasks",
    "load_calendar_entries",
    "query_calendar",
    "resolve_stage",
]
