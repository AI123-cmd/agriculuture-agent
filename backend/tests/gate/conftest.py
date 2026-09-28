"""闸门测试的共用夹具。

闸门规则的输入全部来自 `GateContext`，因此这里提供三个构造器：最小事实源实现、
一条已加载红线、以及一个"全部合法"的 `GateContext`。各规则测试在此基础上按需覆盖字段，
不重复搭脚手架。
"""

from datetime import date

import pytest

from app.gate.contracts import (
    GateAction,
    GateContext,
    GatePayload,
    GatePesticideItem,
    GatePrescription,
)
from app.gate.enums import ProcessId
from app.gate.ports import KnowledgeEntrySnapshot
from app.process_config.redlines import LoadedRedlines, Redline
from app.process_config.rule_ids import GateRuleId


class FakeEntry:
    """`KnowledgeEntrySnapshot` 的最小实现。"""

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


def loaded_redlines(
    process_id: ProcessId = ProcessId.RICE,
    *,
    forbidden: list[str] | None = None,
    forbidden_phrases: list[str] | None = None,
) -> LoadedRedlines:
    """构造一条已加载的红线。默认禁用清单为空，覆盖传入时才有内容。"""
    payload: dict = {}
    if forbidden:
        payload["forbidden"] = forbidden
    if forbidden_phrases:
        payload["forbidden_phrases"] = forbidden_phrases
    return LoadedRedlines(
        process_id=process_id,
        version=1,
        redlines=(
            Redline(
                rule_ids=(GateRuleId.REDLINE,),
                id=f"{process_id.value.upper()}-01",
                desc="禁用清单",
                severity="block",
                payload=payload,
            ),
        ),
        loaded=True,
    )


def unavailable_redlines(process_id: ProcessId = ProcessId.RICE) -> LoadedRedlines:
    return LoadedRedlines.unavailable(process_id, 1, "配置文件缺失")


def prescription(
    *,
    items: list[GatePesticideItem] | None = None,
    actions: list[GateAction] | None = None,
    safety_interval_days: int | None = 7,
    max_uses_per_season: int | None = None,
    facts_provenance: dict[str, str] | None = None,
    detail_terms: dict[str, str] | None = None,
) -> GatePrescription:
    """构造只读处方镜像。默认给一条合法药剂与一条带停止条件的动作。"""
    if items is None:
        items = [
            GatePesticideItem(
                knowledge_id="K-1001",
                name="吡虫啉",
                dosage_min=15.0,
                dosage_max=20.0,
                dosage_unit="克",
                safety_interval_days=7,
                max_uses_per_season=2,
            )
        ]
    if actions is None:
        actions = [
            GateAction(
                order=1,
                action="建议喷施吡虫啉",
                timing="晴天傍晚",
                method="叶面均匀喷雾",
                cautions=["避免高温施药"],
                stop_conditions=["出现药害斑立即停用"],
            )
        ]
    return GatePrescription(
        diagnosis_summary="示例诊断",
        actions=actions,
        safety_interval_days=safety_interval_days,
        max_uses_per_season=max_uses_per_season,
        pesticide_items=items,
        facts_provenance=facts_provenance if facts_provenance is not None else {},
        detail_terms=detail_terms if detail_terms is not None else {},
    )


def gate_context(**overrides) -> GateContext:
    """构造一个默认全部合法的 `GateContext`。各测试按需覆盖字段。"""
    base: dict = {
        "process_id": ProcessId.RICE,
        "config_version": 1,
        "prescription": prescription(),
        "payload": GatePayload(rendered_text="建议在傍晚喷施，若出现药害立即停用。"),
        "redlines": loaded_redlines(),
        "slots": {},
        "knowledge": FakeKnowledge(),
        "medication_history": [],
        "region_code": None,
        "today": date(2025, 6, 1),
        "season_start": None,
        "rule_configs": {},
    }
    base.update(overrides)
    return GateContext(**base)


@pytest.fixture
def ctx() -> GateContext:
    return gate_context()
