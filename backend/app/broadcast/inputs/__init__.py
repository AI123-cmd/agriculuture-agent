"""三类输入的采集入口。

本包把三类输入（环境事实、档案与农事历、问诊与回访）各自隔离成一个模块，
使 `snapshot.py` 的编排层只依赖"采集函数"这一层抽象，不关心取数细节。
"""

from app.broadcast.inputs.calendar import (
    NO_CALENDAR_PROCESSES,
    CalendarEntry,
    collect_calendar_tasks,
    load_calendar_entries,
    resolve_stage,
)
from app.broadcast.inputs.consult import (
    build_followup_prompt,
    build_followup_rule,
    collect_consult_signals,
)
from app.broadcast.inputs.env_facts import EnvFacts, collect_env_facts, is_env_risk_blocked

__all__ = [
    "NO_CALENDAR_PROCESSES",
    "CalendarEntry",
    "EnvFacts",
    "build_followup_prompt",
    "build_followup_rule",
    "collect_calendar_tasks",
    "collect_consult_signals",
    "collect_env_facts",
    "is_env_risk_blocked",
    "load_calendar_entries",
    "resolve_stage",
]
