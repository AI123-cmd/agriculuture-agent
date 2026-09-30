"""问诊与回访信号采集（T3-04，技术方案 11.2）。

本模块把两类"农户问过的事"变成首页提示条目：

1. **观察期问诊**：`status == observing`、只有观察建议没有处方、且距上次交互已满该流程
   观察期阈值的会话。它回答的是"你前两天问的叶片斑点现在怎么样了"——没有处方，因此
   `build_followup_prompt` 只问"有没有变化"，绝不问"效果如何"（问效果等于告诉农户系统
   记错了：他上次拿到的只是一句"再观察看看"）。
2. **到期回访**：`followup_task` 中 `status == pending` 且 `due_at` 已到期的任务。它携带
   处方卡给出的询问语与内容规则，是本项与 T3-09 的接口。

两个来源互斥：`consult_repo.list_observing` 用 `NOT EXISTS` 排除"已登记回访任务"的会话，
使已开方并有回访计划的那类问诊只由回访来源负责，农户不会在同一天看到两条内容几乎相同的
提示（观察期提示与回访提示），那会削弱信任。

三处落地口径：

1. **观察期阈值来自流程配置**：`FollowupPolicy.observation_days`（默认 2，与"先观察两天
   再看看"的产品文案一致）。判定由 `Consultation.needs_followup_check(today, observation_days)`
   完成，阈值由本模块从配置取出后传入，模型不自行读配置。
2. **签名带 `db`（守 L4）**：技术方案 11.2 的骨架写作 `collect_consult_signals(user_id)`，
   但两类来源都要查库且本仓库禁止在 `app/broadcast/` 里直接构造 ORM 查询。因此本模块照
   `calendar.py` 的既有做法把会话前移为显式参数：`collect_consult_signals(db, user_id, today, ...)`。
   调用方（T3-08 的 `_build_snapshot`）本就持有会话。
3. **去重在采集侧，且判据是 `rule_id`**：`rule_id` 对回访类条目取
   `{content_rule or 'followup.effect_check'}:{task_id}` 形态以保证逐任务唯一（技术方案
   11.2 第三段）。去重不查快照表（那是 T3-08 的职责，T3-04 不引入快照仓储），而是把
   "同日快照已含的 rule_id 集合"作为参数注入，使本函数可与快照解耦、单独测试。
"""

from collections.abc import Collection, Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import ProcessId
from app.broadcast.rules.base import RuleResult
from app.models import Consultation, FollowupTask
from app.models.enums import FollowupKind
from app.process_config.loader import load_all_configs
from app.process_config.models import ProcessConfig

# 提示条目来源标记（T3-01 的 `PromptItem.source_kind`）。
FOLLOWUP_SOURCE_KIND = "followup"

# 观察期阈值取不到流程配置时的兜底天数：与 `FollowupPolicy.observation_days` 的默认值一致。
DEFAULT_OBSERVATION_DAYS = 2

# 回访类条目的默认级别。观察期问诊固定 reminder（软性跟进，最轻），不在此表内。
_DEFAULT_FOLLOWUP_SEVERITY = "notice"

# `kind` → `severity` 映射。这是分类逻辑而非文案，故写在这里而不是流程配置里。
# `preserve_scene`（药害固定现场）到 urgent：错过表现期就失去鉴别依据（T3-09 杂草药害分支）。
_SEVERITY_BY_KIND: dict[str, str] = {
    FollowupKind.PRESERVE_SCENE.value: "urgent",
    FollowupKind.EFFECT_CHECK.value: "notice",
}

# 回访类条目的规则标识兜底前缀（`content_rule` 为空时用）。
_EFFECT_CHECK_RULE = "followup.effect_check"


def _severity_of(task: FollowupTask) -> str:
    """按回访种类给级别。未登记的 `kind` 落到默认 `notice`。"""
    return _SEVERITY_BY_KIND.get(task.kind, _DEFAULT_FOLLOWUP_SEVERITY)


def _observation_days(configs: dict[ProcessId, ProcessConfig], process_id: str) -> int:
    """取该流程的观察期阈值；流程标识非法或配置缺失时回到默认值。

    不做"配置读不到就跳过该条"的处理：观察期问诊是本路输入的一半，因配置问题整条丢弃
    会让本已积压的问诊更无人跟进。取不到时用默认阈值继续产出，更符合"让首页记得住农户"。
    """
    try:
        config = configs.get(ProcessId(process_id))
    except ValueError:
        return DEFAULT_OBSERVATION_DAYS
    if config is None:
        return DEFAULT_OBSERVATION_DAYS
    return config.followup_policy.observation_days


def build_followup_prompt(c: Consultation, today: date | None = None) -> RuleResult:
    """观察期问诊条目：只询问现状，不询问"效果"（尚无处方，无效果可问）。

    `callout` 取对象显示名（如"水稻·3亩"）而非流程名：多块地的农户必须看到"哪块地"。
    `fact` 用当时问的主题摘要（`topic_summary`），而非"上次的建议"——农户上次拿到的只是
    一句观察建议，写"上次建议的做法"会让他以为系统记错了。`today` 可注入以固定天数，测试
    用；缺省时取系统当天。
    """
    return RuleResult(
        source_kind=FOLLOWUP_SOURCE_KIND,
        severity="reminder",
        callout=c.object_label,
        fact=f"你 {c.days_ago(today)} 天前问的{c.topic_summary}",
        impact="当时建议先观察两天，现在可以再看看情况",
        action="看看现在和之前比有没有变化",
        rule_id=f"followup.observing.{c.process_id}",
        facts_provenance={"consultation_id": c.id},
    )


def build_followup_rule(f: FollowupTask) -> RuleResult:
    """到期回访条目：按处方卡的 `content_rule` 定规则标识，携带 `task_id` 供前端定位。

    `rule_id` 追加 `:{task_id}`：同一 `content_rule` 会被多次处方复用（如两次杂草药害都指向
    `weed.damage_evidence_checklist`），不带任务号会让 T3-08 按 `rule_id` 做差集合并时把两条
    不同任务的提示折叠成一条。`facts_provenance["task_id"]` 是农户在首页点"回填"时前端定位
    对应任务的依据（产品说明书 4.2.3 功能点 7）。
    """
    return RuleResult(
        source_kind=FOLLOWUP_SOURCE_KIND,
        severity=_severity_of(f),
        callout=f.object_label,
        fact=f.ask,
        impact="上次给的做法是否起作用，是判断要不要调整的依据",
        action="回填一下效果",
        rule_id=f"{f.content_rule or _EFFECT_CHECK_RULE}:{f.id}",
        facts_provenance={"task_id": f.id},
    )


async def collect_consult_signals(
    db: AsyncSession,
    user_id: str,
    today: date,
    *,
    existing_rule_ids: Collection[str] = (),
    configs: dict[ProcessId, ProcessConfig] | None = None,
) -> list[RuleResult]:
    """问诊信号：观察期问诊 + 到期回访，两类合并为 `followup` 类条目。

    `existing_rule_ids` 是"同日快照已含的 `rule_id`"——回访任务在农户回填前会一直处于
    `pending`、`due_at` 当天会被反复命中，若不去重，每次"更新提示"都会让同日条目数增长。
    去重放在本项而非 T3-08 的幂等锁里：锁解决"整份快照是否重生成"，本项解决"同一信号是否
    重复成条"，层次不同。

    数据访问一律走仓储（`consult_repo` / `followup_repo`），不在本模块内直接构造 ORM 查询
    （守 L4）。仓储在函数内延迟导入：`app.repositories` 与 `app.broadcast` 之间不建立模块级
    依赖，与 `calendar.py` 的 `load_calendar_entries` 同一做法。
    """
    from app.repositories import consult_repo, followup_repo

    configs = configs if configs is not None else load_all_configs()
    seen = set(existing_rule_ids)
    signals: list[RuleResult] = []

    observing: Sequence[Consultation] = await consult_repo.list_observing(db, user_id)
    for c in observing:
        if not c.needs_followup_check(today, _observation_days(configs, c.process_id)):
            continue
        item = build_followup_prompt(c, today)
        if item.rule_id in seen:
            continue
        signals.append(item)

    due: Sequence[FollowupTask] = await followup_repo.list_due(db, user_id, today)
    for f in due:
        item = build_followup_rule(f)
        if item.rule_id in seen:
            continue
        signals.append(item)

    return signals


__all__ = [
    "DEFAULT_OBSERVATION_DAYS",
    "FOLLOWUP_SOURCE_KIND",
    "build_followup_prompt",
    "build_followup_rule",
    "collect_consult_signals",
]
