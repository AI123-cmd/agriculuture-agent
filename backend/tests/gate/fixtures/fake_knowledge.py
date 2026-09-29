"""闸门规则测试共用的事实源与红线假实现（T2-35、T2-42）。

规则只依赖 `KnowledgeFacts` 协议与 `LoadedRedlines` 结构，因此单测喂几行假实现即可，
不牵动数据库与配置加载。`FakeEntry` 的默认值取一条"全部合法"的已核准药剂，各用例按需
覆盖字段；红线构造器默认禁用清单为空，避免用例无意间命中而误判。
"""

from app.gate.enums import ProcessId
from app.gate.ports import KnowledgeEntrySnapshot
from app.process_config.redlines import LoadedRedlines, Redline
from app.process_config.rule_ids import GateRuleId


class FakeEntry:
    """`KnowledgeEntrySnapshot` 的最小实现。默认是一条已核准的吡虫啉。"""

    def __init__(
        self,
        knowledge_id: str = "K-1001",
        name: str = "吡虫啉",
        status: str = "approved",
        registered_crops: tuple[str, ...] = ("茶", "水稻"),
        region_scope: str | None = None,
        dosage_min: float | None = 10.0,
        dosage_max: float | None = 30.0,
        dosage_unit: str | None = "克",
        safety_interval_days: int | None = 7,
        max_uses_per_season: int | None = 2,
        forbidden_combinations: tuple[str, ...] = (),
    ) -> None:
        self.knowledge_id = knowledge_id
        self.name = name
        self.status = status
        self.registered_crops = registered_crops
        self.region_scope = region_scope
        self.dosage_min = dosage_min
        self.dosage_max = dosage_max
        self.dosage_unit = dosage_unit
        self.safety_interval_days = safety_interval_days
        self.max_uses_per_season = max_uses_per_season
        self.forbidden_combinations = forbidden_combinations


class FakeKnowledge:
    """`KnowledgeFacts` 的最小实现：闸门只依赖协议声明的方法。"""

    def __init__(self, entries: dict[str, FakeEntry] | None = None) -> None:
        self._entries = entries if entries is not None else {"K-1001": FakeEntry()}

    def get_entry(self, knowledge_id: str) -> KnowledgeEntrySnapshot | None:
        return self._entries.get(knowledge_id)

    def is_approved(self, knowledge_id: str) -> bool:
        entry = self._entries.get(knowledge_id)
        return entry is not None and entry.status == "approved"

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        entry = self._entries.get(knowledge_id)
        return entry.registered_crops if entry is not None else ()


def make_redline(
    process_id: ProcessId = ProcessId.RICE,
    *,
    redline_id: str | None = None,
    desc: str = "禁用清单",
    severity: str = "block",
    forbidden: list[str] | None = None,
    forbidden_phrases: list[str] | None = None,
    rule_ids: tuple[GateRuleId, ...] = (GateRuleId.REDLINE,),
) -> Redline:
    """构造一条红线。`severity` 取配置层的两值之一（block / warn）。"""
    payload: dict = {}
    if forbidden:
        payload["forbidden"] = forbidden
    if forbidden_phrases:
        payload["forbidden_phrases"] = forbidden_phrases
    return Redline(
        rule_ids=rule_ids,
        id=redline_id or f"{process_id.value.upper()}-01",
        desc=desc,
        severity=severity,
        payload=payload,
    )


def loaded_redlines(
    process_id: ProcessId = ProcessId.RICE,
    *,
    forbidden: list[str] | None = None,
    forbidden_phrases: list[str] | None = None,
    redline_id: str | None = None,
    desc: str = "禁用清单",
    severity: str = "block",
    rule_ids: tuple[GateRuleId, ...] = (GateRuleId.REDLINE,),
) -> LoadedRedlines:
    """构造一条已加载的红线。默认禁用清单为空，覆盖传入时才有内容。"""
    return LoadedRedlines(
        process_id=process_id,
        version=1,
        redlines=(
            make_redline(
                process_id,
                redline_id=redline_id,
                desc=desc,
                severity=severity,
                forbidden=forbidden,
                forbidden_phrases=forbidden_phrases,
                rule_ids=rule_ids,
            ),
        ),
        loaded=True,
    )


def redlines_from(
    process_id: ProcessId = ProcessId.RICE,
    *redlines: Redline,
) -> LoadedRedlines:
    """由若干条红线组装成一个已加载结果，用于"block 与 warn 混合"的场景。"""
    return LoadedRedlines(process_id=process_id, version=1, redlines=tuple(redlines), loaded=True)


def unavailable_redlines(process_id: ProcessId = ProcessId.RICE, reason: str = "配置文件缺失") -> LoadedRedlines:
    return LoadedRedlines.unavailable(process_id, 1, reason)


__all__ = [
    "FakeEntry",
    "FakeKnowledge",
    "loaded_redlines",
    "make_redline",
    "redlines_from",
    "unavailable_redlines",
]
