"""播报生成器：规则优先 + 农事历补充 + AI 兜底。"""

from dataclasses import asdict
from datetime import date

from app.broadcast.calendar import phase_tasks
from app.broadcast.rules import all_rules
from app.broadcast.rules.base import RuleResult
from app.broadcast.skill import BroadcastSkill
from app.broadcast.snapshot import build_snapshot
from app.core.config import get_settings
from app.repositories import Repositories
from app.tools import profile


async def generate_broadcast(repos: Repositories | None, user_id: str) -> list[RuleResult]:
    """生成当日播报提醒列表：规则命中直出 → 农事历补充 → AI 兜底。"""
    settings = get_settings()
    snap = await build_snapshot(repos, user_id)

    hits = [res for rule in all_rules() if (res := rule.match(snap, settings))]
    hits += phase_tasks(snap.crop_name, snap.growth_stage, date.today().month)

    if not hits:
        profile_data = await profile.query_profile(repos, user_id) if repos is not None else {}
        hits = [await BroadcastSkill().brief(snap, profile_data)]

    # 附上环境快照便于追溯
    snap_meta = asdict(snap)
    for r in hits:
        r.meta = snap_meta
    return hits


__all__ = ["generate_broadcast"]
