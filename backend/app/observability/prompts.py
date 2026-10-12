"""generation span 的提示词身份解析（T6-07，技术方案 15.5 与 14.4）。

generation span 上要写 `prompt.name` / `prompt.version` / `prompt.is_fallback` 三个属性，
提示词版本对比才能按"环节 + 版本"聚合。版本号来源必须**唯一**：T2-60 的提示词注册表
（`app.agent.prompts.registry`）。业务侧的 `prompt_snapshot` 与研发侧的 Langfuse 对比若各自
取版本，会出现"业务说用的是 v7、观测数据显示 v5"这类无法对账的情况（T6-07 第四节）。

### 为什么解析放在可观测性而不是网关里

网关属 `app/llm/`，受全局约定 L3 约束：**不得导入任何业务模块**，而提示词注册表在
`app.agent.prompts`（它又依赖 `app.agent.state`）。把解析放在本模块（横切关注点，不受 L1-L4
约束），网关只调 `record_prompt_identity`，依赖方向仍是"通用组件 → 横切关注点"，L3 成立。

### 版本 0 的含义

`observe` 与 `panel` 的提示词目前是智能体模块内的内联构造，尚未迁入注册表，故取不到版本。
这类情况记 `0` 而不是缺省属性：属性缺失会让后端按 `prompt.version` 分组时整类记录消失，
比"取到 0（未登记版本）"更难排查。注册表未登记的模板名同理记 0 而不抛 `KeyError`——
观测路径不得因为一个新增模板、漏登记版本号而打断问诊。
"""

from __future__ import annotations

from opentelemetry.trace import Span

from app.observability.attributes import PROMPT_IS_FALLBACK, PROMPT_NAME, PROMPT_VERSION

# 网关任务类型（`app/llm/enums.py` 的 `TaskType` 取值）→ 注册表模板名。
# 键刻意用字符串字面量而不是导入 `app.llm.enums`：本模块要能被任何一层使用，且网关的
# 任务类型是镜像枚举（T2-57），用取值字符串做键不会随枚举位置变动而失效。
TASK_PROMPT_NAMES: dict[str, str] = {
    "triage": "triage",
    "observe": "observe",
    "interview": "interviewer",
    "retrieve": "retriever",
    "diagnose": "diagnoser",
    "prescribe": "prescriber",
    "panel": "panel",
    "snapshot_writer": "snapshot_writer",
    "report_guard": "report_guard",
}

# 未登记版本的占位值。见模块说明：不省略属性，用 0 表示"未登记"。
UNREGISTERED_VERSION = 0


def prompt_name_of(task_type: str) -> str:
    """任务类型对应的提示词模板名。未登记的任务类型回退为任务类型本身。

    回退而不是空串：后端按 `prompt.name` 分组时，一个陌生但可读的名字（如新增任务类型
    忘了登记映射）比空串更容易定位。
    """
    return TASK_PROMPT_NAMES.get(task_type, task_type)


def prompt_version_of(name: str) -> int:
    """取模板名的当前版本号。未登记或注册表不可导入时返回 `UNREGISTERED_VERSION`。

    延迟导入 `app.agent.prompts.registry`：模块导入期导入会让 `app.observability` 在
    `app.agent` 包初始化完成前就拉住它，与 `app.agent.agents.base` 对 `app.observability`
    的导入形成环。
    """
    try:
        from app.agent.prompts.registry import current_prompt_version

        return int(current_prompt_version(name))
    except (ImportError, KeyError):
        return UNREGISTERED_VERSION


def record_prompt_identity(span: Span, task_type: str, *, is_fallback: bool) -> None:
    """把提示词身份三属性写进 generation span。

    `is_fallback` 的口径：本次调用走了**非首选候选或降档候选**。技术方案原文把它描述为
    "是否走了降级模板"，但代码现状里没有"降级模板"这一概念——降级出口（模板问题、
    规则兜底）根本不发起模型调用，因而不会产生 generation span。当前能在网关处观测到的
    最接近信号就是候选链回退与降档，故按此口径实现并在文档中固定下来。
    """
    name = prompt_name_of(task_type)
    span.set_attribute(PROMPT_NAME, name)
    span.set_attribute(PROMPT_VERSION, prompt_version_of(name))
    span.set_attribute(PROMPT_IS_FALLBACK, bool(is_fallback))


__all__ = [
    "TASK_PROMPT_NAMES",
    "UNREGISTERED_VERSION",
    "prompt_name_of",
    "prompt_version_of",
    "record_prompt_identity",
]
