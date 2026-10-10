"""分诊契约（技术方案 5.3.2）。

分诊智能体的**唯一产出**是流程标识与缺口列表，它**不得生成任何问题**——问题由问诊
智能体在 T2-16 生成。这条硬约束由本模块的字段结构保证：`TriageResult` 里没有任何
放置问题文本的字段，生成问题的能力因此不存在于契约中。

失败策略与现场采集相反：分诊失败（无法归属到具体流程）**回退通用流程**，且必须
给出 `fallback_reason`；现场采集失败不阻断问诊，只标注数据不可用（见 `observation.py`）。
"""

from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.state import ProcessId
from app.process_config.models import ProcessConfig

# 枚举声明序，用于别名命中长度相同时的稳定裁决（见 `match_object_alias`）。
_PROCESS_ORDER: dict[ProcessId, int] = {pid: i for i, pid in enumerate(ProcessId)}


class TriageResult(BaseModel):
    """分诊输出。技术方案 5.3.2 原样定义。

    `object_label` 是**有格式约定的字符串**（如"水稻·3亩"、"2号栏舍·120头"）而非结构化
    字段：它唯一的用途是回显给农户确认，结构化只会增加无用的解析负担。
    """

    model_config = ConfigDict(extra="forbid")

    process_id: ProcessId
    object_confirmed: bool
    object_label: str
    slot_gaps: list[str]
    profile_ready: bool
    fallback_reason: str | None = None
    handoff_to_report: bool = False

    @model_validator(mode="after")
    def _fallback_requires_reason(self) -> "TriageResult":
        """降级到 `general` 不是"没匹配上就默认"，而是一个需要说明理由的决策。

        有了 `fallback_reason`，线上指标里"哪些表述容易分流失败"就有据可查，而不只是
        看到一个模糊的 general 占比。
        """
        if self.process_id is ProcessId.GENERAL and not self.fallback_reason:
            raise ValueError("降级到 general 必须给出 fallback_reason")
        return self

    @model_validator(mode="after")
    def _pig_handoff_consistency(self) -> "TriageResult":
        """`handoff_to_report` 的语义很窄：养猪流程以"不漏报"为最高目标，在 `observe`
        之前就插入报告守卫。若杂草流程也能置它为 True，主图会在无关场景跳过现场采集。
        """
        if self.handoff_to_report and self.process_id is not ProcessId.PIG:
            raise ValueError("handoff_to_report 仅养猪流程可置为 True")
        return self


class ObjectConfirmRequest(BaseModel):
    """对象待确认时插入的一次交互。

    `profile_process_id` 与 `matched_alias` 是两个诊断信息：前者用于生成"您档案里登记的
    是水稻，这次问的是茶吗"这类问句，后者用于线上统计"哪些别名容易漏配"。二者都不进入
    面向农户的最终文案，只作为提示词与统计的输入。
    """

    model_config = ConfigDict(extra="forbid")

    asked_process_id: ProcessId  # 分诊初步判定的流程
    profile_process_id: ProcessId | None  # 档案中记录的流程
    candidate_labels: list[str]  # 供农户选择的对象名
    question_text: str  # 面向农户的确认问句
    matched_alias: str | None = None  # 命中的同义词，便于复盘


def match_object_alias(text: str, configs: dict[ProcessId, ProcessConfig]) -> ProcessId | None:
    """按各流程 `object_aliases` 做最长别名优先匹配，未命中返回 `None`。

    "最长别名优先"是为了处理别名互相包含的情形：茶树流程的别名含"茶"，若 farmer 说的是
    一个更长的、属于别的流程的词，按最长匹配才能路由正确。

    命中长度相同时按 `ProcessId` 的**声明序**取靠前者，而不是按枚举值的字典序：声明序是
    业务上的优先级（`RICE` 先于 `TEA`），且它是源码里可见、可评审的固定顺序，字典序则
    会随流程改名而变动。两种做法都满足"同一输入永远得到同一结果"的可重放要求，选用前者
    的额外好处是裁决依据可读。

    纯函数，无 I/O 与随机性，可被单元测试完全覆盖，也保证路由结果可确定性重放。
    """
    hits: list[tuple[int, int, ProcessId]] = []
    for pid, config in configs.items():
        for alias in config.object_aliases:
            if alias and alias in text:
                hits.append((len(alias), -_PROCESS_ORDER[pid], pid))
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][2]


def resolve_process_hint(raw: str | None, configs: dict[ProcessId, ProcessConfig]) -> ProcessId | None:
    """把入口直达提示归一为流程标识，未命中返回 `None`。

    入口提示有两套取值来源，必须都接受：一是小程序「作物模型」按钮传的**农户可见作物名**
    （如「水稻」「辣椒」），二是直达流程按钮传的 **`ProcessId` 英文值**（如「weed」）。
    直接 `ProcessId(raw)` 遇到中文名会抛 `ValueError`，使接口 500，因此不能只做精确匹配。

    英文值走精确匹配；其余复用各流程配置的 `object_aliases`（流程配置是唯一事实来源）
    做别名匹配，使「水稻 → rice」这类映射无需在客户端重复维护。同一个函数既服务此处的
    入口归一，也与 `match_object_alias` 共用裁决口径（最长别名优先、声明序并列裁决）。

    未命中一律返回 `None`，由调用方决定兜底（服务层回退 `general`）——「哪些作物没有
    专属流程」是业务事实，应当显式可见，而不是在此静默给出一个看似合理的结果。
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    try:
        return ProcessId(text)
    except ValueError:
        return match_object_alias(text, configs)


__all__ = ["ObjectConfirmRequest", "TriageResult", "match_object_alias", "resolve_process_hint"]
