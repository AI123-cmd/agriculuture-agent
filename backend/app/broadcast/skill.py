"""AI 兜底：规则均未命中时，用通用农业模型生成当日农事简报。"""

from app.ai_router.registry import resolve
from app.broadcast.rules.base import RuleResult
from app.broadcast.snapshot import EnvSnapshot


class BroadcastSkill:
    """复用通用农业模型生成简报，逻辑与诊断垂类隔离。"""

    async def brief(self, snapshot: EnvSnapshot, profile: dict | None = None) -> RuleResult:
        model = resolve("通用农业")
        env = {
            "crop": snapshot.crop_name,
            "growth_stage": snapshot.growth_stage,
            "temp": snapshot.temp,
            "humidity": snapshot.humidity,
            "soil_moisture": snapshot.soil_moisture,
            "forecast": snapshot.forecast.get("summary", "") if snapshot.forecast else "",
        }
        context = {"farmer": (profile or {}).get("farmer") or {}, "environment": env}
        message = (
            "请根据以下环境快照生成一份简洁的今日农事简报（150字以内），"
            "覆盖：环境概况、需注意的风险点、建议的农事操作。直接输出正文，不要标题与编号。"
            f"\n环境快照：{env}"
        )
        content = await model.chat(message, context)
        return RuleResult(
            type="ai_brief",
            level="normal",
            title="今日农事简报",
            content=content[:500],
            source="ai",
            action="结合简报安排当日农事操作",
        )
