"""回访计划构造（T2-21）。

回访时点只有一个来源：流程配置的 `followup_policy.triggers`。本模块不硬编码任何天数，
换流程即换回访节奏，改配置不改代码。

唯一的例外是杂草流程的药害分支。产品说明书 4.2.3 指出药害纠纷需要"保护现场"提示，
因为药害鉴定要求保留典型性状，错过表现期将无法鉴别起因。这条回访不是"按天计的效果
询问"，而是"立刻要做的现场固定动作"，因此它不写进 `followup_policy`（配置里的 `after_days`
无法表达"立即"这一语义），由 `branch == "herbicide_damage"` 直接构造 `due_offset_days=0`。

药害分支下不叠加常规回访：农户已经进入纠纷处理路径，此时再给一条"7 天后看看药效如何"
会造成"你既让我保护现场又让我观察效果"的矛盾。
"""

from typing import cast

from app.agent.contracts import FollowupPlan, FollowupTask
from app.agent.contracts.prescription import FollowupKindValue
from app.agent.state import ProcessId
from app.process_config.loader import load_process_config

# 判定与分诊给出的分支标识。药害分支优先于流程配置的回访策略。
HERBICIDE_DAMAGE_BRANCH = "herbicide_damage"

# 药害分支的内容规则 id，供文案层取"现场固定动作清单"模板。
_DAMAGE_EVIDENCE_RULE = "weed.damage_evidence_checklist"
_DAMAGE_ASK = "现在请先保护现场并拍照记录，不要清理"


def build_followup_plan(process_id: ProcessId, branch: str | None) -> FollowupPlan:
    """回访计划：按流程取值，药害分支立即触发。

    `due_offset_days` 用相对天数而非绝对日期：处方可能在任意时刻生成，而回访时点总是
    相对于动作。转成绝对时间戳是回访调度服务（T3-09）在登记任务时一次完成的。
    """
    if branch == HERBICIDE_DAMAGE_BRANCH:
        return FollowupPlan(
            tasks=[
                FollowupTask(
                    due_offset_days=0,
                    kind="preserve_scene",
                    content_rule=_DAMAGE_EVIDENCE_RULE,
                    ask=_DAMAGE_ASK,
                )
            ]
        )
    policy = load_process_config(process_id).followup_policy
    return FollowupPlan(
        tasks=[
            FollowupTask(
                due_offset_days=trigger.after_days,
                kind=cast(FollowupKindValue, trigger.kind),
                ask=trigger.ask,
                window_based=policy.window_based,
            )
            for trigger in policy.triggers
        ]
    )


__all__ = ["HERBICIDE_DAMAGE_BRANCH", "build_followup_plan"]
