"""提示词模板包（T2-60）。

按技术方案 9.4 的"结构化拼装"组织提示词：角色定位、输出契约、硬规则、术语规范、禁用输出、
易混淆项、候选问题池七部分分开构造，各智能体的 `build_messages` 从这里取构造入口。构造过程
是纯 Python（f-string 与 join），不引入模板引擎；`formatters` 里的函数全是纯函数，输入配置
对象、输出字符串，因此提示词可被完整单测。

版本号见 `registry.PROMPT_VERSIONS`，`current_prompt_version(name)` 供网关调用上下文与评测
记录读取，使"改了提示词之后效果变好了还是变差了"可被按版本分组回答。

边界：本包只含"如何组织语言"与"有哪些约束"，**不写任何农技事实**（药剂名、剂量、阈值）。
事实由知识库检索后注入候选区，或由代码在处方里填充。
"""

from app.agent.prompts.diagnoser import build_diagnoser_prompt
from app.agent.prompts.formatters import (
    format_candidates,
    format_colloquial,
    format_confusables,
    format_diagnosis,
    format_dosage,
    format_entries,
    format_evidence,
    format_history,
    format_observations,
    format_pesticide_candidates,
    format_process_keywords,
    format_redlines,
    format_slots,
    format_terms,
)
from app.agent.prompts.interviewer import TOP_K, build_interviewer_prompt
from app.agent.prompts.prescriber import build_prescriber_prompt
from app.agent.prompts.registry import PROMPT_VERSIONS, PromptTemplate, current_prompt_version
from app.agent.prompts.triage import build_triage_prompt

__all__ = [
    "PROMPT_VERSIONS",
    "TOP_K",
    "PromptTemplate",
    "build_diagnoser_prompt",
    "build_interviewer_prompt",
    "build_prescriber_prompt",
    "build_triage_prompt",
    "current_prompt_version",
    "format_candidates",
    "format_colloquial",
    "format_confusables",
    "format_diagnosis",
    "format_dosage",
    "format_entries",
    "format_evidence",
    "format_history",
    "format_observations",
    "format_pesticide_candidates",
    "format_process_keywords",
    "format_redlines",
    "format_slots",
    "format_terms",
]
