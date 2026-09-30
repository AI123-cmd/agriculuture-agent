"""提示词模板注册与版本号管理（T2-60）。

版本号是模块级常量，每次修改模板必须递增。技术方案 9.4 要求"每次提示词模板变更记录版本号，
与 `process_config.version` 绑定"。绑定的实现方式是在计量与评估记录里同时写下 `prompt_version`
与 `config_version`，使评估服务能按这两个维度分组：只改提示词时按 `prompt_version` 分组，
只改配置时按 `config_version` 分组。

`current_prompt_version(name)` 供网关的调用上下文与评测记录读取。评估按版本分组是"改了提示词
之后效果变好了还是变差了"可被量化回答的前提，没有这层绑定，提示词改动就是不可归因的。

### 不引入模板引擎

模板用 Python f-string 与 join 构造，不用 Jinja2 之类的外部模板引擎。原因是拼装逻辑本身就是
代码（含条件、含从配置取值），用模板引擎反而要把这些逻辑塞进模板语言，更难测试与调试。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agent.state import ProcessId

# 模板版本号。七个名字覆盖全部模板：分诊、问诊、检索改写、判定、处方、报告守卫、快照生成。
# `report_guard` 与 `snapshot_writer` 目前是纯代码路径不构造提示词，但仍登记版本号：评估服务
# 按名字分组统计时需要它们的占位版本，缺登记会让"这个名字是否改过"无法回答。
PROMPT_VERSIONS: dict[str, int] = {
    "triage": 1,
    "interviewer": 1,
    "retriever": 1,
    "diagnoser": 1,
    "prescriber": 1,
    "report_guard": 1,
    "snapshot_writer": 1,
}


class PromptTemplate(BaseModel):
    """一个提示词模板。版本与流程配置版本绑定。

    `process_id=None` 表示共用模板（如分诊）。`build` 是构造回调：入参至少含流程配置与状态，
    具体智能体可追加候选等额外入参。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    version: int
    process_id: ProcessId | None = None
    build: Callable[..., list[dict[str, Any]]]


def current_prompt_version(name: str) -> int:
    """取某个模板名的当前版本号。未登记的模板名抛 `KeyError` 而不是返回默认值。

    静默返回默认值会让"新增了模板却忘了登记版本"变成不可见的事实——评估服务会一直按一个
    不存在的版本号分组，看起来"这个模板从没改过"。
    """
    return PROMPT_VERSIONS[name]


__all__ = ["PROMPT_VERSIONS", "PromptTemplate", "current_prompt_version"]
