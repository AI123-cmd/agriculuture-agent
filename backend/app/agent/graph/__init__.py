"""图编排包（T2-24 起）。

`routes.py` 存放全部条件边函数与 path_map；`main.py`（T2-25）是主图组装；
`factories.py`（T2-26）是通用节点工厂；`subgraphs/`（T2-27 起）是七条流程子图；
`gate_graph.py`（T2-45）是闸门子图。

本包只做导出，不在导入期拉起任何外部依赖：`routes` 可在纯内存下导入与单测，
`main` 与 `subgraphs` 才需要模型网关与流程配置。
"""

from app.agent.graph.routes import (
    PANEL_ENABLED_PROCESSES,
    ROUTE_AFTER_CONFIRM_OBJECT,
    ROUTE_AFTER_GATE,
    ROUTE_AFTER_INTAKE,
    ROUTE_AFTER_INTERVIEW,
    ROUTE_AFTER_JUDGE,
    ROUTE_AFTER_REPORT_GUARD,
    ROUTE_AFTER_SUBGRAPH,
    ROUTE_AFTER_SUBGRAPH_GUARD,
    ROUTE_AFTER_TRIAGE,
    ROUTE_MAPS,
    UnroutedStateError,
    route_after_confirm_object,
    route_after_gate,
    route_after_intake,
    route_after_interview,
    route_after_judge,
    route_after_report_guard,
    route_after_subgraph,
    route_after_subgraph_guard,
    route_after_triage,
    slots_ready,
)

__all__ = [
    "PANEL_ENABLED_PROCESSES",
    "ROUTE_AFTER_CONFIRM_OBJECT",
    "ROUTE_AFTER_GATE",
    "ROUTE_AFTER_INTAKE",
    "ROUTE_AFTER_INTERVIEW",
    "ROUTE_AFTER_JUDGE",
    "ROUTE_AFTER_REPORT_GUARD",
    "ROUTE_AFTER_SUBGRAPH",
    "ROUTE_AFTER_SUBGRAPH_GUARD",
    "ROUTE_AFTER_TRIAGE",
    "ROUTE_MAPS",
    "UnroutedStateError",
    "route_after_confirm_object",
    "route_after_gate",
    "route_after_intake",
    "route_after_interview",
    "route_after_judge",
    "route_after_report_guard",
    "route_after_subgraph",
    "route_after_subgraph_guard",
    "route_after_triage",
    "slots_ready",
]
