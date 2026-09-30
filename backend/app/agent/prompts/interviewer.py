"""问诊提示词构造（T2-60，技术方案 9.4 的七步拼装）。

七步固定顺序：前三部分是"这个智能体是谁、输出什么、有什么硬约束"，中间三部分是"按流程
差异化的术语、红线、鉴别点"，最后一部分是"本次的候选池"。把流程差异集中在第 4 到 6 部分，
让"新增一条流程"只需新增 YAML，提示词自动包含新流程的术语与红线，不需要改提示词代码。

第 2 部分注入 `InterviewBatch.model_json_schema()` 是结构化输出的第二层（提示词约束），与网关
把 schema 传适配器的第一层配合，两层任一层成功即可。

第 7 部分是候选问题池，由代码筛选后注入（`select_candidate_slots`），模型只能在给定范围内
选择问题方向。这让"跳过已知项"与"不漏必问项"由代码保证，模型只负责语言表达。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.contracts import InterviewBatch
from app.agent.prompts.formatters import (
    CandidateView,
    format_candidates,
    format_colloquial,
    format_confusables,
    format_observations,
    format_redlines,
    format_slots,
    format_terms,
)
from app.agent.state import ConsultationState
from app.process_config.models import ProcessConfig

# 候选池容量。技术方案 5.3.3 定为 6：每轮最多问两个，六个候选足够两轮；更长的候选列表会让
# 模型的选择变发散（技术方案 10.4 指出长上下文会降低指令遵循度）。
TOP_K = 6


def build_interviewer_prompt(
    config: ProcessConfig,
    state: ConsultationState,
    *,
    candidates: Any = None,
    region_code: str | None = None,
    top_k: int = TOP_K,
) -> list[dict[str, Any]]:
    """拼装问诊智能体的提示词：结构化字段注入，便于按流程差异化。

    前六段进 system（角色定位、输出契约、硬规则、术语规范、禁用输出、易混淆项），第七段
    （候选问题池）进 user。候选未显式传入时由本函数调用 `select_candidate_slots` 计算，
    使"给一份配置与状态即可构造提示词"成立；传入候选时用调用方算好的池，避免重复计算。

    候选方向以文本注入而不是让模型自己看 `slots` 推断，是这条链路能收敛的前提：模型只需
    在给定的几个方向里挑，不必自己算"哪些还没问过"。
    """
    pool = candidates if candidates is not None else _select_candidates(config, state, top_k=top_k)
    candidate_list: list[CandidateView] = list(pool)
    system = [
        f"你是{config.role_definition}。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(InterviewBatch.model_json_schema(), ensure_ascii=False)}",
        "每轮最多问 2 个问题。每个问题必须说明为什么问。",
        "不得询问已经知道答案的信息。",
        "农户表示不清楚的项目不要换个说法再问一遍。",
        f"使用以下术语：{format_terms(config.terminology.display_metrics)}",
        f"面向农户的表述必须口语化：{format_colloquial(config.terminology.colloquial_map)}",
        f"严禁输出：{format_redlines(config.redlines)}",
        f"你必须主动追问下列鉴别点：{format_confusables(config.confusable_items)}",
    ]
    user = [
        f"农户所在地区：{region_code or '未知'}",
        f"已确认信息：{format_slots(config, state)}",
        f"当前环境事实：{format_observations(state.get('observations'))}",
        f"候选追问方向（从中选择，不得超出）：\n{format_candidates(candidate_list)}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


def _select_candidates(config: ProcessConfig, state: ConsultationState, *, top_k: int) -> list[CandidateView]:
    """候选池由 T2-17 的筛选器算出。延迟导入：筛选器位于 `agents` 包，模块级导入会与 `agents`
    包对 `prompts` 的导入形成环。"""
    from app.agent.agents.candidate_pool import select_candidate_slots

    return list(select_candidate_slots(config, state, top_k=top_k).candidates)


__all__ = ["TOP_K", "build_interviewer_prompt"]
